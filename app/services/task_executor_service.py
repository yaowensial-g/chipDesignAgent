"""Serial hierarchical Run driver with durable decision and action checkpoints."""

import asyncio
import hashlib
import json
import logging
import os
import re
from uuid import uuid4

from pydantic import ValidationError

from app.agents.runtime import runtime
from app.domain.enums import AgentRole, Mutability, ToolRunStatus
from app.domain.models import ChatRequest, ChatResponse, RunResumeRequest, RunView, TaskIntent, ToolRunRequest, ToolRunView, ToolSelection
from app.domain.orchestration import (
    TASK_CONTROLS, Artifact, ArtifactPayload, AuthorizedAction, EvidenceRef, LintResult, Observation, ProfessionalRef, ReflectionRecord,
    ReplanTrigger, RunExecutionPolicy, TaskActionContext, TaskActionResult, TaskContract, TaskOutputRef, TaskPlan,
    TaskPlanDraft, TaskState, VerificationResult,
)
from app.models.provider import ModelInvocationError
from app.services.context_builder_service import ContextBuilderService
from app.services.eda_workflow import eda_workflow
from app.services.global_planner_service import GlobalPlannerService, InvalidPlan, PlanningPolicy
from app.services.replanner_service import ReplannerService
from app.services.run_service import RunService
from app.services.task_action_service import TaskActionService
from app.services.task_verifier_service import TaskVerifierService
from app.services.token_usage_service import token_usage_tracker
from app.services.tool_selector_service import ToolSelectorService
from app.storage.orchestration_repository import OrchestrationRepository, StaleExecution, now
from app.tools.builtin import verilator_available, verilator_enabled
from app.tools.adapters.verilator_adapter import VERILATOR_TOOLS
from app.tools.eda_catalog import EDA_TOOLS
from app.tools.project_manifest import FrozenProject


logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}
TERMINAL_ACTION_ERRORS = {
    "capability_gap", "interrupted_unknown", "approval_rejected", "approval_target_changed", "invalidation_rejected", "run_action_budget",
}
NON_REPLAN_ERRORS = {
    "tool_unavailable", "task_scope_denied", "expert_scope_denied", "workspace_scope_denied", "workspace_required",
    "rtl_path_scope_denied", "rtl_source_missing", "analysis_unavailable", "lint_scope_denied", "eda_scope_denied",
    "protected_tool_argument", "confirmed_ds_required", "action_budget", "stale_action",
} | TERMINAL_ACTION_ERRORS
REPLAYABLE = {"read_rtl_source", *TASK_CONTROLS}
READ_ONLY_TOOLS = {
    AgentRole.DESIGN: ("read_rtl_source", "analyze_rtl_design", "submit_task_output"),
    AgentRole.VERIFICATION: ("assess_rtl_verification", "submit_task_output"),
}
LINT_REQUEST = re.compile(
    r"(?:执行|运行|使用|启动|\brun\b|\bexecute\b|\bperform\b)\s*(?:真实\s*)?(?:verilator\b|lint\b|静态检查)",
    re.IGNORECASE,
)
NEGATED_REQUEST = re.compile(r"(?:不|不要|无需|无须|禁止|\bdo not|\bdon't|\bnever)\s*$", re.IGNORECASE)


class HierarchicalModeDisabled(ValueError):
    pass


class TaskExecutorService:
    def __init__(
        self, runs: RunService, repo: OrchestrationRepository, contexts: ContextBuilderService,
        selector: ToolSelectorService, actions: TaskActionService, *,
        planner: GlobalPlannerService | None = None, agent_runtime=runtime,
    ) -> None:
        self.runs = runs
        self.repo = repo
        self.contexts = contexts
        self.selector = selector
        self.actions = actions
        self.actions.tools = runs.tools
        self.actions.patches = runs.patches
        self.runtime = agent_runtime
        self.planner = planner or GlobalPlannerService(actions.registry)
        self.replanner = ReplannerService(self.planner)
        self.verifier = TaskVerifierService(runs.tools)
        self._driving: set[str] = set()

    async def handle(self, request: ChatRequest) -> ChatResponse:
        if os.getenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "").lower() not in {"1", "true"}:
            raise HierarchicalModeDisabled("分层编排开发入口尚未启用。")
        if request.orchestration_mode != "rtl_analysis" and (request.run_lint or request.lint_top_module):
            raise ValueError("真实 Lint 仅支持 rtl_analysis 入口。")
        session_id = request.session_id or str(uuid4())
        session = self.repo.sessions.resolve(session_id, request.project_id, request.user_role, request.module_name)
        if request.orchestration_mode == "eda_project":
            eda_workflow(request, session.primary_agent)
        self._check_workspace(request)
        run_id = self.repo.create(session_id, session.primary_agent, request)
        run = await self.drive(run_id)
        return ChatResponse(
            session_id=session_id, assistant_message=self._message(run),
            route="orchestrated", route_reason=run.route_reason, run=run,
        )

    def _run(self, run_id: str) -> RunView:
        run = self.runs.get(run_id)
        if run is None or run.engine_version != "hierarchical":
            raise KeyError(run_id)
        return run

    async def resume(self, run_id: str, decision: RunResumeRequest) -> RunView:
        run = self._run(run_id)
        if decision.action == "cancel":
            return self.cancel(run_id)
        if not decision.wait_id:
            raise ValueError("Resume requires wait_id")
        if decision.action == "provide_resolution" and not decision.content.strip() and not (decision.workspace_id or decision.rtl_path):
            raise ValueError("Resume requires information for the pending task")
        if decision.tool_arguments or len(decision.content) > 8_000:
            raise ValueError("Unsupported resume arguments or oversized response")
        if decision.action in {"approve", "reject"} and (decision.workspace_id or decision.rtl_path):
            raise ValueError("Approval cannot change the recorded Workspace or RTL path")
        checkpoint = self.repo.checkpoint(run_id)
        request = ChatRequest.model_validate_json(checkpoint["request_json"])
        updates = {
            key: value for key, value in {"workspace_id": decision.workspace_id, "rtl_path": decision.rtl_path}.items()
            if value is not None and value != getattr(request, key)
        }
        if updates and checkpoint["plan_json"]:
            raise ValueError("已计划的输入范围保持固定，请为新的 Workspace 或 RTL 路径发起新 Run。")
        request = request.model_copy(update=updates)
        self._check_workspace(request)
        resumed = self.repo.resume(run_id, decision, request, run.primary_agent, run.session_id)
        if not resumed:
            return self._run(run_id)
        return await self._drive_with_usage(run)

    def cancel(self, run_id: str) -> RunView:
        run = self._run(run_id)
        if run_id in self._driving or run_id in self.repo.external_runs() or run.status.value in TERMINAL:
            raise ValueError("Run cannot be cancelled while dispatching or after completion")
        self._finish(run, "cancelled", "已取消当前 Run。")
        return self._run(run_id)

    async def recover(self) -> None:
        for run_id in self.repo.recoverable():
            await self._drive_with_usage(self._run(run_id))

    async def poll_external(self) -> None:
        for run_id in self.repo.external_runs():
            await self._drive_with_usage(self._run(run_id))

    async def run_background(self) -> None:
        await self.recover()
        while True:
            await asyncio.sleep(0.25)
            await self.poll_external()

    async def _drive_with_usage(self, run: RunView) -> RunView:
        token = token_usage_tracker.start()
        try:
            return await self.drive(run.run_id)
        finally:
            usage = token_usage_tracker.finish(token)
            if usage.model_calls:
                self.repo.sessions.record_token_usage(run.session_id, usage, run_id=run.run_id)

    async def drive(self, run_id: str) -> RunView:
        if run_id in self._driving:
            return self._run(run_id)
        self._driving.add(run_id)
        try:
            await self._drive(run_id)
        except StaleExecution:
            logger.info("Ignored superseded hierarchical execution: %s", run_id)
        except Exception:
            logger.exception("Hierarchical Run failed: %s", run_id)
            self._finish(self._run(run_id), "failed", "任务执行遇到内部错误，请使用 Run ID 查看事件。")
        finally:
            self._driving.remove(run_id)
        result = self._run(run_id)
        if result.status.value in TERMINAL or result.pending_user_request:
            self._enqueue_summary(result)
        return result

    async def _drive(self, run_id: str) -> None:
        run = self._run(run_id)
        if run.status.value in TERMINAL or run.pending_user_request:
            return
        checkpoint = self.repo.checkpoint(run_id)
        request = ChatRequest.model_validate_json(checkpoint["request_json"])
        if not checkpoint["plan_json"]:
            if not await self._plan(run, request, checkpoint):
                return
            checkpoint = self.repo.checkpoint(run_id)
        while True:
            checkpoint = self.repo.checkpoint(run_id)
            plan = TaskPlan.model_validate_json(checkpoint["plan_json"])
            if plan.status != "active":
                return
            policy = (PlanningPolicy.model_validate_json(checkpoint["planning_policy_json"])
                      if checkpoint["planning_policy_json"] else None)
            enabled = self._replanning_enabled(request, policy)
            limits = policy.run_limits if policy else RunExecutionPolicy()
            if checkpoint["replan_trigger_json"]:
                trigger = ReplanTrigger.model_validate_json(checkpoint["replan_trigger_json"])
                if not enabled:
                    self._finish(run, "failed", "当前 Run 的增量重规划不可用。", plan_id=plan.plan_id)
                    return
                if not await self._replan(run, plan, policy, trigger):
                    return
                continue
            states = self.repo.states(run_id)
            active_id = checkpoint["active_task_id"]
            if active_id:
                if states[active_id].status == "failed":
                    self._finish(run, "failed", states[active_id].failure_reason or "交付件未通过独立验收。", plan_id=plan.plan_id)
                    return
                contract = next(item for item in plan.tasks if item.task_id == active_id)
                if not await self._step(run, request, plan, contract, states[active_id], limits, enabled):
                    return
            elif all(states[task.task_id].status == "completed" for task in plan.tasks):
                final = self.repo.local_outputs(plan.final_task_id, "verified", states[plan.final_task_id].generation)
                report = next(item for item in final if item.artifact.artifact_type == "report")
                self.verifier._check_content(report)
                results = self._project_results(run_id) if request.orchestration_mode == "eda_project" else []
                status = "failed" if any(item["status"] != "PASS" for item in results) else "completed"
                self._finish(run, status, report.content.decode("utf-8"), report.artifact.artifact_id, plan_id=plan.plan_id)
                return
            else:
                contract = next((
                    node for node in plan.tasks if states[node.task_id].status == "pending"
                    and all(states[parent].status == "completed" for parent in node.depends_on)
                ), None)
                if contract is None:
                    self._finish(run, "failed", "没有可执行任务，依赖或必需交付件未满足。")
                    return
                try:
                    self.repo.claim(states[contract.task_id], contract, plan.plan_id)
                except StaleExecution:
                    raise
                except ValueError:
                    self._finish(run, "failed", "下游任务缺少已验证的必需输入。")
                    return

    async def _plan(self, run: RunView, request: ChatRequest, checkpoint: dict) -> bool:
        if request.orchestration_mode == "eda_project":
            return self._plan_eda(run, request)
        is_rtl = request.orchestration_mode == "rtl_analysis"
        wants_lint = is_rtl and (request.run_lint or any(
            not NEGATED_REQUEST.search(request.message[:match.start()]) for match in LINT_REQUEST.finditer(request.message)
        ))
        if wants_lint:
            if not verilator_available():
                detail = "真实 Verilator Lint 未启用。" if not verilator_enabled() else "未找到可用的 Verilator 可执行文件。"
                self._finish(run, "failed", detail, blocked=True)
                return False
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", request.lint_top_module or request.module_name):
                self._finish(run, "failed", "请为真实 Lint 指定合法的 lint_top_module。")
                return False
        if is_rtl and not (request.workspace_id and request.rtl_path):
            self._wait_for_inputs(run, "请指定同项目的 Workspace 和一个 RTL 相对路径。")
            return False
        if is_rtl:
            try:
                self._check_workspace(request)
                if not self.actions.workspaces.resolve_path(request.workspace_id, request.rtl_path).is_file():
                    self._wait_for_inputs(run, "未找到指定 RTL 文件。请添加文件或补充正确的相对路径。")
                    return False
            except (KeyError, ValueError):
                self._finish(run, "failed", "RTL 路径超出可访问的 Workspace。")
                return False
        snapshot = json.loads(checkpoint["snapshot"])
        if not checkpoint["context_id"]:
            root = self.repo.sessions.get_message(checkpoint["root_message_id"])
            source_request = ChatRequest.model_validate({**request.model_dump(), "documents": snapshot["uploads"]})
            self._check_documents(source_request)
            context = await asyncio.to_thread(
                self.contexts.build, run.session_id, source_request, run.primary_agent.value,
                root.message_id, before_sequence=root.sequence, pinned_version_ids=tuple(source_request.document_ids),
            )
            manifest = self.contexts.save_manifest(run.session_id, run.run_id, run.primary_agent.value, context)
            snapshot = context.effective_request.model_dump(
                mode="json", include={"history", "documents", "project_id", "module_name", "message"},
            )
            snapshot["_memory_context"] = context.memory_context
            snapshot["_context_manifest"] = {
                "context_id": manifest.context_id, "message_ids": manifest.message_ids,
                "memory_ids": manifest.memory_ids, "chunk_ids": manifest.chunk_ids,
            }
            self.repo.save_snapshot(run.run_id, manifest.context_id, snapshot)
        decisions = self.repo.decisions(run.run_id)
        payload = {**snapshot, "message": request.message, "clarifications": [item["content"] for item in decisions]}
        try:
            intent = TaskIntent.model_validate(await asyncio.to_thread(
                self.runtime.enrich_intent, payload, list(READ_ONLY_TOOLS[run.primary_agent]),
            ))
        except ModelInvocationError:
            self._finish(run, "failed", "意图识别所需的模型当前不可用。", blocked=True)
            return False
        if intent.missing_information and not decisions:
            self._wait_for_inputs(run, "请补充：" + "；".join(intent.missing_information)[:1000])
            return False
        tools = dict(READ_ONLY_TOOLS) if is_rtl else {run.primary_agent: ("submit_task_output",)}
        required_tools = ("read_rtl_source", "analyze_rtl_design", "assess_rtl_verification") if is_rtl else ()
        mutability = (Mutability.READ, Mutability.ANALYSIS)
        if wants_lint:
            tools[run.primary_agent] = (*tools[run.primary_agent], "verilator_lint")
            required_tools += ("verilator_lint",)
            mutability += (Mutability.EXECUTION,)
        try:
            limit = int(os.getenv("CHIP_AGENT_MAX_PLAN_TASKS", "8"))
            limits = RunExecutionPolicy(
                max_plan_tasks=limit, max_replans=int(os.getenv("CHIP_AGENT_MAX_REPLANS", "2")),
                max_run_actions=int(os.getenv("CHIP_AGENT_MAX_RUN_ACTIONS", "192")),
                max_run_decisions=int(os.getenv("CHIP_AGENT_MAX_RUN_DECISIONS", "384")),
            )
        except ValueError:
            self._finish(run, "failed", "计划节点或执行预算配置无效。")
            return False
        policy = PlanningPolicy(
            primary_agent=run.primary_agent, allowed_tools=tools, run_limits=limits,
            required_tools=required_tools, allowed_mutability=mutability,
        )
        if is_rtl:
            payload["workflow_contract"] = {
                "rtl_path": request.rtl_path,
                "design": "Design: read_rtl + rtl_design_analysis; required rtl_snapshot and design_analysis outputs",
                "verification": "Verification: rtl_verification_assessment; bind rtl_snapshot and design_analysis from Design, output verification_assessment",
                "report": "Primary role: compose_report; bind design_analysis and verification_assessment, output one text/markdown report",
                "formats": {"rtl_snapshot": "text/plain", "design_analysis": "application/json", "verification_assessment": "application/json"},
                "rule": "required_output",
            }
            if wants_lint:
                payload["workflow_contract"]["lint"] = (
                    "Add exactly one separate primary-role task with capability real_rtl_lint. "
                    "Set read_only=false and allow only analysis and execution. Bind rtl_snapshot from Design, "
                    "output one required lint_result as application/json. Final report must depend on it and bind lint_result."
                )
        proposal = await asyncio.to_thread(
            self.planner.plan, run.run_id, request, intent, payload, policy,
            simple_question=not is_rtl,
            validate_workflow=(lambda plan: self._validate_rtl_plan(plan, wants_lint=wants_lint)) if is_rtl else None,
        )
        if proposal.plan is None:
            with self.repo.db.transaction() as conn:
                for code in proposal.diagnostics:
                    self.repo.event(conn, run.run_id, "planning_diagnostic", code)
            self._finish(run, "failed", proposal.message, blocked=proposal.status == "capability_gap")
            return False
        self.repo.save_plan(proposal.plan, policy.model_dump_json())
        return True

    def _plan_eda(self, run: RunView, request: ChatRequest) -> bool:
        if not request.workspace_id:
            self._wait_for_inputs(run, "请指定包含工程清单的 Workspace。")
            return False
        draft, arguments = eda_workflow(request, run.primary_agent)
        policy = PlanningPolicy(
            primary_agent=run.primary_agent,
            allowed_tools={run.primary_agent: (*arguments, "submit_task_output")},
            required_tools=tuple(arguments),
            allowed_mutability=(Mutability.READ, Mutability.ANALYSIS, Mutability.EXECUTION, Mutability.CANDIDATE_WRITE),
            run_limits=RunExecutionPolicy(max_plan_tasks=int(os.getenv("CHIP_AGENT_MAX_PLAN_TASKS", "8"))),
        )
        result = self.planner.plan(
            run.run_id, request, TaskIntent(raw_request=request.message, goal=request.message), {}, policy,
            fixed_draft=draft,
        )
        if result.plan is None:
            available = {tool.name for tool in self.actions.registry.for_role(run.primary_agent, task=True)}
            missing = ", ".join(name for name in arguments if name not in available)
            self._finish(
                run, "failed", f"工程能力不可用：{missing}。" if missing else result.message,
                blocked=result.status == "capability_gap",
            )
            return False
        self.repo.save_plan(result.plan)
        return True

    @staticmethod
    def _validate_rtl_plan(plan: TaskPlanDraft, *, wants_lint: bool = False, allow_auxiliary: bool = False) -> None:
        by_id = {node.task_id: node for node in plan.tasks}
        final = by_id[plan.final_task_id]
        design = [node for node in plan.tasks if {"read_rtl", "rtl_design_analysis"} <= set(node.capability_requirements)]
        review = [node for node in plan.tasks if "rtl_verification_assessment" in node.capability_requirements]
        if ((not allow_auxiliary and len(plan.tasks) != 3 + int(wants_lint)) or len(design) != 1 or len(review) != 1
            or len({design[0].task_id, review[0].task_id, final.task_id}) != 3):
            raise InvalidPlan("rtl_requires_design_verification_and_report")
        design, review = design[0], review[0]
        output_types = (
            (design, {"rtl_snapshot": "text/plain", "design_analysis": "application/json"}),
            (review, {"verification_assessment": "application/json"}),
            (final, {"report": "text/markdown"}),
        )
        required_bindings = (
            (review, {(design.task_id, "rtl_snapshot"), (design.task_id, "design_analysis")}),
            (final, {(design.task_id, "design_analysis"), (review.task_id, "verification_assessment")}),
        )
        if any(not node.constraints.read_only for node in (design, review, final)):
            raise InvalidPlan("rtl_analysis_nodes_must_be_read_only")
        lint_nodes = [node for node in plan.tasks if "real_rtl_lint" in node.capability_requirements]
        if len(lint_nodes) != int(wants_lint):
            raise InvalidPlan("rtl_lint_requirement_changed")
        if wants_lint:
            lint = lint_nodes[0]
            if (
                lint.task_id in {design.task_id, review.task_id, final.task_id}
                or lint.assigned_agent != final.assigned_agent or lint.constraints.read_only
                or set(lint.capability_requirements) != {"real_rtl_lint"}
                or set(lint.constraints.allowed_mutability) != {Mutability.ANALYSIS, Mutability.EXECUTION}
                or not lint.execution_policy.max_action_steps
            ):
                raise InvalidPlan("invalid_lint_task_scope")
            output_types += ((lint, {"lint_result": "application/json"}),)
            required_bindings += (
                (lint, {(design.task_id, "rtl_snapshot")}),
                (final, {(lint.task_id, "lint_result")}),
            )
        if any(
            len(node.outputs) != len(required) or any(
                not output.required or required.get(output.artifact_type) != output.media_type for output in node.outputs
            ) or len({output.artifact_type for output in node.outputs}) != len(required)
            for node, required in output_types
        ):
            raise InvalidPlan("rtl_output_contract_mismatch")
        if "compose_report" not in final.capability_requirements:
            raise InvalidPlan("rtl_report_capability_missing")
        for node, required in required_bindings:
            actual = {
                (item.source.task_id, item.artifact_type) for item in node.inputs
                if item.required and isinstance(item.source, TaskOutputRef)
            }
            if not required <= actual:
                raise InvalidPlan("rtl_verified_inputs_missing")
        if allow_auxiliary:
            protected = {node.task_id for node, _ in output_types}
            supported = {"rtl_snapshot": "text/plain", "report": "text/markdown"}
            for node in plan.tasks:
                if node.task_id in protected:
                    continue
                required = {item.artifact_type for item in node.outputs if item.required}
                if (not node.constraints.read_only or any(supported.get(item.artifact_type) != item.media_type for item in node.outputs)
                    or ("rtl_snapshot" in required and "read_rtl" not in node.capability_requirements)
                    or ("report" in required and "compose_report" not in node.capability_requirements)):
                    raise InvalidPlan("unsupported_rtl_auxiliary_contract")

    async def _step(
        self, run: RunView, request: ChatRequest, plan: TaskPlan, contract: TaskContract, state: TaskState,
        limits: RunExecutionPolicy, replanning_enabled: bool,
    ) -> bool:
        last = self.repo.last_action(state.task_id, state.generation)
        result = json.loads(last["result_json"]) if last and last["status"] == "done" else {}
        error_code = result.get("error_code")
        if error_code in TERMINAL_ACTION_ERRORS or result.get("outcome") == "awaiting_external":
            self._finish(run, "failed", result["summary"], blocked=error_code == "capability_gap", plan_id=plan.plan_id)
            return False
        inputs = None
        if replanning_enabled and state.phase != "awaiting_external" and error_code != "lint_failed":
            inputs = self.repo.bound_inputs(state.task_id, state.generation)
            damaged = []
            for item in inputs:
                try:
                    self.verifier._check_content(item)
                except ValueError:
                    damaged.append(item.artifact.artifact_id)
            if damaged:
                self.repo.request_replan(plan, state, ReplanTrigger(
                    kind="artifact_invalid", task_id=state.task_id, reason="已验收输入的存储内容与原始证据不一致。",
                    invalidated_artifact_ids=tuple(damaged),
                    evidence=tuple(EvidenceRef(kind="artifact", ref_id=key) for key in damaged),
                ))
                return True
        if state.phase == "verifying":
            return self._verify(run, plan, contract, state, last, replanning_enabled)
        if error_code == "task_failure_reported":
            if result.get("plan_issue") and replanning_enabled:
                handled = self._handle_plan_issue(plan, state, last["action_id"], TaskActionResult.model_validate(result), inputs or (), limits)
                if handled is not None:
                    return handled
            self._finish(run, "failed", result["summary"], plan_id=plan.plan_id)
            return False
        if last and last["status"] in {"selecting", "selected", "dispatching"}:
            if last["status"] == "selecting":
                self._record(state, last["action_id"], "decision", "unknown", "模型决策在服务中断前未写入。")
                return True
            if last["status"] == "dispatching" and json.loads(last["selection_json"])["name"] not in REPLAYABLE | {"verilator_lint"} | VERILATOR_TOOLS:
                message = "上次工程动作在服务中断后无法确认结果。"
                delegate = self.actions.delegations.reconcile_interrupted(run.run_id, last["action_id"])
                interrupted = TaskActionResult(
                    outcome="failed", summary=message, error_code="interrupted_unknown",
                    professional_refs=(ProfessionalRef(kind="delegation", object_id=delegate),) if delegate else (),
                )
                self._record(state, last["action_id"], json.loads(last["selection_json"])["name"], "unknown", message, result=interrupted)
                self._finish(run, "failed", message)
                return False
            return await self._dispatch(run, request, contract, state, last, limits)
        used_actions, used_decisions = self.repo.usage(run.run_id)
        if state.current_iteration >= contract.execution_policy.max_decisions or used_decisions >= limits.max_run_decisions:
            self._finish(run, "failed", "任务或 Run 决策额度已耗尽，交付件未通过验收。", plan_id=plan.plan_id)
            return False

        state, action_id = self.repo.reserve(state)
        observations = self.repo.observations(state.task_id, state.generation)
        candidates = self.repo.local_outputs(state.task_id, "staged", state.generation)
        if inputs is None:
            inputs = self.repo.bound_inputs(state.task_id, state.generation)
        for item in (*inputs, *candidates):
            self.verifier._check_content(item)
        verification = self.repo.last_verification(state.task_id, state.generation)
        payload = self._decision_context(run, request, contract, observations, inputs, candidates, verification)
        if replanning_enabled:
            payload["plan_issue_guidance"] = (
                "有确切工程证据显示当前计划缺少前置、依赖错误或路径不可行时，"
                "可在 report_task_failure 中附带 plan_issue。仅引用可见的当前证据。"
                "怀疑已验收输入错误时提供 artifact_invalid 和具体输入 Artifact ID。"
            )
            payload["plan_tasks"] = [{"task_id": task.task_id, "title": task.title} for task in plan.tasks]
            if state.generation > 1:
                previous = self.repo.last_verification(state.task_id, state.generation - 1)
                if previous:
                    feedback = VerificationResult.model_validate_json(previous["result_json"])
                    if not feedback.passed:
                        payload["previous_attempt_feedback"] = {
                            "failed_criteria": feedback.failed_criteria, "suggestions": feedback.suggestions,
                        }
        evidence = (
            *(EvidenceRef(kind="observation", ref_id=item.observation_id) for item in observations),
            *(EvidenceRef(kind="artifact", ref_id=item.artifact.artifact_id) for item in (*inputs, *candidates)),
        )
        self._save_task_manifest(run, state, contract, payload, evidence)
        permitted = self._permitted_tools(contract)
        controls_only = (
            state.current_iteration >= contract.execution_policy.max_decisions
            or state.action_count >= contract.execution_policy.max_action_steps
            or used_actions >= limits.max_run_actions
            or ("real_rtl_lint" in contract.capability_requirements and state.action_count > 0)
            or (bool(VERILATOR_TOOLS.intersection(contract.capability_requirements)) and state.action_count > 0)
            or ("propose_eda_patch" in contract.capability_requirements and bool(candidates))
        )
        try:
            shortlist, _ = await asyncio.to_thread(
                self.selector.select_for_task, contract, payload, permitted_tools=permitted, controls_only=controls_only,
            )
            reflection, selection = await asyncio.to_thread(
                self.runtime.reflect_and_select, contract, payload, shortlist, evidence,
            )
            reflection.validate_decision(contract, selection.name, evidence)
            if selection.name not in {tool.name for tool in shortlist}:
                raise ValueError("Selected tool is outside shortlist")
        except (ValueError, TimeoutError, ModelInvocationError):
            self._record(state, action_id, "decision", "denied", "模型决策或工具选择未通过校验。")
            return True
        self.repo.select(state, selection, [tool.name for tool in shortlist], ReflectionRecord(
            reflection_id=str(uuid4()), run_id=state.run_id, task_id=state.task_id, generation=state.generation,
            decision_sequence=state.current_iteration, action_id=action_id,
            observation_ids=tuple(item.observation_id for item in observations),
            verification_id=verification["verification_id"] if verification else None, result=reflection, created_at=now(),
        ))
        return await self._dispatch(run, request, contract, state, self.repo.last_action(state.task_id, state.generation), limits)

    async def _dispatch(
        self, run: RunView, request: ChatRequest, contract: TaskContract, state: TaskState, action: dict, limits: RunExecutionPolicy,
    ) -> bool:
        selection = ToolSelection.model_validate_json(action["selection_json"])
        name = selection.name
        replay = action["status"] == "dispatching"
        counted = name not in TASK_CONTROLS
        if counted and not replay and self.repo.usage(run.run_id)[0] >= limits.max_run_actions:
            result = TaskActionResult(outcome="failed", summary="Run 工具动作额度已耗尽。", error_code="run_action_budget")
            self._record(state, action["action_id"], name, "denied", result.summary, result=result)
            return True
        lint = name == "verilator_lint"
        eda = name in VERILATOR_TOOLS
        external = lint or eda
        link = self.runs.tools.frozen_tool_link(action["action_id"]) if external else None
        if link:
            tool_run = self.runs.tools.reconcile_frozen_tool(link["tool_run_id"])
            if tool_run.status != ToolRunStatus.QUEUED:
                self.repo.mark_external(state, tool_run.run_id, name)
                if eda:
                    return self._collect_eda(state, contract, action["action_id"], tool_run, json.loads(link["target_json"]))
                return self._collect_lint(state, contract, action["action_id"], tool_run, json.loads(link["target_json"]))
        context = TaskActionContext(
            run_id=state.run_id, generation=state.generation, action_id=action["action_id"], contract=contract,
            primary_agent=run.primary_agent, project_id=run.project_id, module_name=run.module_name,
            workspace_id=request.workspace_id, allowed_rtl_paths=(request.rtl_path,) if request.rtl_path else (),
            permitted_tools=self._permitted_tools(contract),
            lint_top_module=(request.lint_top_module or request.module_name) if lint else None,
            eda_arguments=eda_workflow(request, run.primary_agent)[1] if request.orchestration_mode == "eda_project" else {},
            inputs=(*self.repo.bound_inputs(state.task_id, state.generation),
                    *self.repo.local_outputs(state.task_id, "staged", state.generation)),
            action_count=state.action_count - int(replay and counted),
        )
        shortlist = [
            tool for tool in self.actions.registry.for_role(contract.assigned_agent, task=True)
            if tool.name in json.loads(action["shortlist_json"])
        ]
        approval = self.repo.approved_action(action["action_id"]) if external else None
        result = self.actions.authorize_task_action(context, selection, shortlist, approval)
        if external and approval and not isinstance(result, AuthorizedAction):
            if link:
                self.runs.tools.cancel_queued_tool(link["tool_run_id"])
            message = "已批准的工程目标或环境发生变化，请重新发起 Run。"
            result = TaskActionResult(outcome="failed", summary=message, error_code="approval_target_changed")
        staged = []
        if isinstance(result, AuthorizedAction):
            if not replay:
                state = self.repo.dispatch(state, action["action_id"], counted)
            if external:
                job = ToolRunRequest(
                    workspace_id=context.workspace_id, role=contract.assigned_agent, tool_name=name,
                    arguments=selection.arguments, user_approved=True,
                )
                if eda:
                    target, project = self.actions.eda_target(context, selection)
                    tool_run = self.runs.tools.start_frozen_eda(action["action_id"], job, target, project)
                    self.repo.mark_external(state, tool_run.run_id, name)
                    return self._collect_eda(state, contract, action["action_id"], tool_run, target)
                target, content = self.actions.lint_target(context, selection)
                tool_run = self.runs.tools.start_frozen_lint(action["action_id"], job, target, content)
                self.repo.mark_external(state, tool_run.run_id, name)
                return self._collect_lint(state, contract, action["action_id"], tool_run, target)

            def stage(key: str, content: bytes, metadata: dict) -> Artifact:
                item = self.repo.staged_payload(state, contract, action["action_id"], key, content, metadata)
                staged.append(item)
                return item.artifact

            result = await self.actions.execute_task_action(context, result, stage)
        if result.outcome == "observed" and staged:
            provenance = result.evidence or tuple(
                EvidenceRef(kind="artifact", ref_id=item.artifact.artifact_id)
                for item in self.repo.bound_inputs(state.task_id, state.generation)
            )
            staged = [
                item.model_copy(update={"artifact": item.artifact.model_copy(update={"evidence": provenance})})
                for item in staged
            ]
        pending = result.wait.model_dump(mode="json") if result.wait else None
        if lint and pending:
            target, _ = self.actions.lint_target(context, selection)
            pending["lint_target"] = {
                key: target[key] for key in ("snapshot_ref", "content_hash", "relative_path", "top_module", "version")
            }
            pending["message"] = f"批准 Verilator Lint：{target['relative_path']}，顶层 {target['top_module']}，SHA-256 {target['content_hash']}。"
        if eda and pending:
            target, _ = self.actions.eda_target(context, selection)
            pending["eda_target"] = {
                key: target[key] for key in ("snapshot_ref", "content_hash", "manifest_path", "top_module", "version", "arguments")
            }
            pending["message"] = f"批准 {name}：工程输入 SHA-256 {target['content_hash']}。"
        status = "waiting" if pending else "failed" if result.outcome == "failed" else "succeeded"
        self._record(
            state, action["action_id"], name, status, result.summary, result=result,
            staged=staged if status == "succeeded" else [], pending=pending,
            verify=name == "submit_task_output" and result.outcome == "observed",
        )
        if pending:
            return False
        return True

    @staticmethod
    def _replanning_enabled(request: ChatRequest, policy: PlanningPolicy | None) -> bool:
        return (policy is not None and request.orchestration_mode == "rtl_analysis"
                and os.getenv("CHIP_AGENT_REPLANNING_ENABLED", "").strip().lower() in {"1", "true"})

    async def _replan(self, run: RunView, plan: TaskPlan, policy: PlanningPolicy, trigger: ReplanTrigger) -> bool:
        limits = policy.run_limits
        actions, decisions = self.repo.usage(run.run_id)
        if actions >= limits.max_run_actions or decisions >= limits.max_run_decisions:
            self._finish(run, "failed", f"Run 执行预算已耗尽。{trigger.reason}", plan_id=plan.plan_id)
            return False
        wants_lint = "verilator_lint" in policy.required_tools
        if wants_lint and not verilator_available():
            self._finish(run, "failed", "继续当前计划需要可用的 Verilator Lint。", plan_id=plan.plan_id, blocked=True)
            return False
        request = ChatRequest.model_validate_json(self.repo.checkpoint(run.run_id)["request_json"])
        try:
            self._check_workspace(request)
            if not self.actions.workspaces.resolve_path(request.workspace_id, request.rtl_path).is_file():
                raise FileNotFoundError(request.rtl_path)
        except (KeyError, ValueError, OSError):
            self._finish(run, "failed", "继续当前计划需要原 Workspace 中的 RTL 输入。", plan_id=plan.plan_id, blocked=True)
            return False

        number, feedback = self.repo.reserve_replan(plan, trigger, limits.max_replans)
        if number is None:
            self._finish(run, "failed", f"重规划提案额度已耗尽。{trigger.reason}", plan_id=plan.plan_id)
            return False
        states = self.repo.states(run.run_id)
        delta = None
        try:
            delta = await asyncio.to_thread(self.replanner.propose, self.runtime, plan, policy, trigger, {
                "raw_request": run.user_instruction, "module_name": request.module_name, "rtl_path": request.rtl_path,
                "clarifications": [item["content"] for item in self.repo.decisions(run.run_id) if item["action"] == "provide_resolution"],
                "validation_feedback": feedback,
                "tasks": [self._replan_task_context(states[task.task_id]) for task in plan.tasks],
            })
            with self.repo.db.transaction() as conn:
                def consumers_of(keys: set[str]) -> set[str]:
                    outputs = self.repo.dependencies.outputs(conn, run.run_id, keys)
                    return set(self.repo.dependencies.impact(conn, run.run_id, outputs).consumers)

                revision, reruns, removed = self.replanner.revise(
                    plan, delta, policy, states, trigger, consumers_of=consumers_of,
                    validate_workflow=lambda draft: self._validate_rtl_plan(draft, wants_lint=wants_lint, allow_auxiliary=True),
                )
        except InvalidPlan as exc:
            self.repo.reject_replan(plan, number, str(exc), delta)
            return True
        except (ModelInvocationError, TimeoutError, ValidationError) as exc:
            code = "invalid_delta_shape" if isinstance(exc, ValidationError) else "replanner_output_unavailable"
            self.repo.reject_replan(plan, number, code, delta)
            if isinstance(exc, ModelInvocationError) and exc.code == "MODEL_NOT_CONFIGURED":
                self._finish(run, "failed", f"增量重规划需要可用的主模型。{trigger.reason}", blocked=True, plan_id=plan.plan_id)
                return False
            return True
        self.repo.publish_replan(plan, revision, number, delta, trigger, reruns, removed)
        return True

    def _replan_task_context(self, state: TaskState) -> dict:
        verification = self.repo.last_verification(state.task_id, state.generation)
        return {
            "task_id": state.task_id, "status": state.status, "failure_reason": state.failure_reason,
            "observations": [item.model_dump(mode="json") for item in self.repo.observations(state.task_id, state.generation)[-3:]],
            "bound_inputs": self.repo.input_bindings(state.task_id, state.generation),
            "verification_feedback": json.loads(verification["result_json"]) if verification else None,
            "verified_outputs": [item.artifact.model_dump(mode="json")
                                 for item in self.repo.local_outputs(state.task_id, "verified", state.generation)],
        }

    def _handle_plan_issue(
        self, plan: TaskPlan, state: TaskState, action_id: str, result: TaskActionResult,
        inputs: tuple[ArtifactPayload, ...], limits: RunExecutionPolicy,
    ) -> bool | None:
        issue = result.plan_issue
        observations = self.repo.observations(state.task_id, state.generation)
        known_inputs = {item.artifact.artifact_id: item for item in inputs}
        visible_artifacts = set(known_inputs)
        if issue.issue_type != "artifact_invalid":
            visible_artifacts.update(item.artifact.artifact_id for item in self.repo.local_outputs(state.task_id, "staged", state.generation))
        known_evidence = {
            *(EvidenceRef(kind="artifact", ref_id=key) for key in visible_artifacts),
            *(EvidenceRef(kind="observation", ref_id=item.observation_id) for item in observations
              if item.action_id != action_id and item.status in {"succeeded", "failed"}),
        }
        if (state.task_id not in issue.affected_task_ids
            or not set(issue.affected_task_ids) <= {task.task_id for task in plan.tasks}
            or not set(issue.related_artifact_ids) <= visible_artifacts
            or not set(issue.evidence) <= known_evidence
            or any(item.error in NON_REPLAN_ERRORS for item in observations)):
            with self.repo.db.transaction() as conn:
                self.repo.event(conn, plan.run_id, "plan_issue_rejected", "证据不可用或失败类别无法通过计划变更解决。", task_id=state.task_id)
            return None
        trigger = ReplanTrigger(kind="plan_issue", task_id=state.task_id, reason=issue.reason, evidence=issue.evidence, issue=issue)
        if issue.issue_type == "artifact_invalid":
            if not issue.related_artifact_ids or not set(issue.related_artifact_ids) <= {
                item.ref_id for item in issue.evidence if item.kind == "artifact"
            }:
                return None
            confirmed = []
            for key in dict.fromkeys(issue.related_artifact_ids):
                item = known_inputs[key]
                source = item.artifact
                if self.verifier.confirms_invalidation(
                    item, self.repo.bound_inputs(source.source_task_id, source.source_generation, include_superseded=True),
                    self.repo.local_outputs(source.source_task_id, "verified", source.source_generation),
                ):
                    confirmed.append(key)
            trigger = trigger.model_copy(update={"kind": "artifact_invalid", "invalidated_artifact_ids": (
                tuple(confirmed) if confirmed else tuple(dict.fromkeys(issue.related_artifact_ids))
            )})
            if not confirmed:
                actions, decisions = self.repo.usage(plan.run_id)
                if (self.repo.replan_count(plan.run_id) >= limits.max_replans or actions >= limits.max_run_actions
                    or decisions >= limits.max_run_decisions):
                    return None
                self.repo.request_invalidation_confirmation(state, action_id, trigger)
                return False
        self.repo.request_replan(plan, state, trigger)
        return True

    @staticmethod
    def _permitted_tools(contract: TaskContract) -> tuple[str, ...]:
        if "real_rtl_lint" in contract.capability_requirements:
            return ("verilator_lint",)
        for capability in contract.capability_requirements:
            if capability in EDA_TOOLS:
                return (capability,)
        return READ_ONLY_TOOLS.get(contract.assigned_agent, ())

    def _collect_eda(self, state: TaskState, contract: TaskContract, action_id: str, tool: ToolRunView, target: dict) -> bool:
        if tool.status in {ToolRunStatus.QUEUED, ToolRunStatus.RUNNING, ToolRunStatus.PARSING}:
            return False
        result = self.runs.tools.eda_result(tool.run_id)
        if result is None:
            message = f"ToolRun {tool.run_id} 的执行结果不明。"
            failure = TaskActionResult(outcome="failed", summary=message, error_code="interrupted_unknown")
            self._record(state, action_id, tool.tool_name, "unknown", message, result=failure)
            return True
        evidence = (EvidenceRef(kind="tool_run", ref_id=tool.run_id), EvidenceRef(kind="artifact", ref_id=target["snapshot_ref"]))
        proof = {"tool_run_id": tool.run_id, "result": result.model_dump(mode="json")}
        spec = next(item for item in contract.outputs if item.artifact_type == "eda_result")
        item = self.repo.staged_payload(state, contract, action_id, spec.key, json.dumps(proof).encode(), {})
        item = item.model_copy(update={"artifact": item.artifact.model_copy(update={"evidence": evidence})})
        summary = f"{tool.tool_name}：{result.status}，ToolRun {tool.run_id}。"
        observed = TaskActionResult(
            outcome="observed", summary=summary, candidate_artifact_ids=(item.artifact.artifact_id,),
            evidence=evidence, professional_refs=(ProfessionalRef(kind="tool_run", object_id=tool.run_id),),
        )
        self._record(
            state, action_id, tool.tool_name, "succeeded" if result.status == "PASS" else "failed",
            summary, result=observed, staged=[item],
        )
        return True

    def _collect_lint(self, state: TaskState, contract: TaskContract, action_id: str, tool: ToolRunView, target: dict) -> bool:
        if tool.status in {ToolRunStatus.QUEUED, ToolRunStatus.RUNNING, ToolRunStatus.PARSING}:
            return False
        passed = tool.status == ToolRunStatus.COMPLETED and tool.exit_code == 0
        summary = f"真实 Verilator Lint {'通过' if passed else '未通过'}，ToolRun {tool.run_id}。"
        staged = []
        evidence = (EvidenceRef(kind="tool_run", ref_id=tool.run_id), EvidenceRef(kind="artifact", ref_id=target["snapshot_ref"]))
        if tool.exit_code is not None:
            receipt = LintResult(
                tool_run_id=tool.run_id, snapshot_ref=target["snapshot_ref"], content_hash=target["content_hash"],
                top_module=target["top_module"], version=target["version"], exit_code=tool.exit_code,
                stdout_hash=hashlib.sha256(tool.stdout.encode()).hexdigest(),
                stderr_hash=hashlib.sha256(tool.stderr.encode()).hexdigest(),
            )
            spec = next(item for item in contract.outputs if item.artifact_type == "lint_result")
            payload = self.repo.staged_payload(state, contract, action_id, spec.key, receipt.model_dump_json().encode(), {})
            staged.append(payload.model_copy(update={"artifact": payload.artifact.model_copy(update={"evidence": evidence})}))
        result = TaskActionResult(
            outcome="observed" if passed else "failed", summary=summary, evidence=evidence,
            candidate_artifact_ids=tuple(item.artifact.artifact_id for item in staged),
            professional_refs=(ProfessionalRef(kind="tool_run", object_id=tool.run_id),),
            error_code=None if passed else "lint_failed",
        )
        self._record(
            state, action_id, "verilator_lint", "succeeded" if passed else "failed", summary,
            result=result, staged=staged, verify=not passed,
        )
        return True

    def _record(self, state, action_id, name, status, summary, *, result=None, staged=(), pending=None, verify=False) -> None:
        observation = Observation(
            observation_id=str(uuid4()), run_id=state.run_id, task_id=state.task_id, generation=state.generation,
            action_id=action_id, tool_name=name, status=status, summary=summary,
            evidence=result.evidence if result else (), artifact_ids=result.candidate_artifact_ids if result else (),
            error=result.error_code if result else None, created_at=now(),
        )
        if not self.repo.record_outcome(
            state, observation, result.model_dump(mode="json") if result else {}, list(staged), verify=verify, pending=pending,
        ):
            raise StaleExecution(action_id)

    def _verify(
        self, run: RunView, plan: TaskPlan, contract: TaskContract, state: TaskState, action: dict, replanning_enabled: bool,
    ) -> bool:
        result = TaskActionResult.model_validate_json(action["result_json"])
        ids = set(result.candidate_artifact_ids)
        submitted = tuple(item for item in self.repo.local_outputs(state.task_id, "staged", state.generation)
                          if item.artifact.artifact_id in ids)
        inputs = self.repo.bound_inputs(state.task_id, state.generation)
        verification = self.verifier.verify(contract, submitted, inputs, contract.acceptance_criteria)
        global_result = (
            self.verifier.verify(contract, submitted, inputs, plan.success_criteria)
            if verification.passed and state.task_id == plan.final_task_id else None
        )
        if verification.passed and (global_result is None or global_result.passed):
            completed = state.model_copy(update={
                "status": "completed", "phase": None, "completed_at": now(),
                "artifact_ids": tuple(item.artifact.artifact_id for item in submitted),
            })
            if not self.repo.record_verification(completed, action["action_id"], verification, tuple(ids), global_result=global_result):
                raise StaleExecution(action["action_id"])
            return True
        failure = global_result if global_result is not None and not global_result.passed else verification
        if result.error_code != "lint_failed" and failure.repairable and state.repair_attempts < contract.execution_policy.max_repair_attempts and state.current_iteration < contract.execution_policy.max_decisions:
            repaired = state.model_copy(update={"phase": "repairing", "repair_attempts": state.repair_attempts + 1})
            if not self.repo.record_verification(repaired, action["action_id"], verification, tuple(ids), global_result=global_result):
                raise StaleExecution(action["action_id"])
            return True
        failed = state.model_copy(update={
            "status": "failed", "phase": None, "completed_at": now(), "failure_reason": "交付件未通过独立验收。",
        })
        message = result.summary if result.error_code == "lint_failed" else "交付件未通过独立验收，修正额度已耗尽。"
        trigger = None
        if (replanning_enabled and result.error_code != "lint_failed" and failure.repairable
            and state.repair_attempts >= contract.execution_policy.max_repair_attempts
            and state.current_iteration < contract.execution_policy.max_decisions
            and not any(item.error in NON_REPLAN_ERRORS for item in self.repo.observations(state.task_id, state.generation))):
            trigger = ReplanTrigger(kind="repair_exhausted", task_id=state.task_id, reason=message, evidence=failure.evidence)
        if not self.repo.record_verification(failed, action["action_id"], verification, tuple(ids), global_result=global_result, trigger=trigger):
            raise StaleExecution(action["action_id"])
        if trigger:
            return True
        self._finish(run, "failed", message, plan_id=plan.plan_id)
        return False

    def _decision_context(self, run, request, contract, observations, inputs, candidates, verification) -> dict:
        payload = {
            "message": contract.goal,
            "rtl_path": request.rtl_path if "read_rtl" in contract.capability_requirements else None,
            "observations": [item.model_dump(mode="json") for item in observations],
            "artifacts": [{
                **item.artifact.model_dump(mode="json"),
                "text": self._artifact_summary(item),
            } for item in (*inputs, *candidates)],
            "verification_feedback": json.loads(verification["result_json"]) if verification else None,
            "output_guidance": "报告在 submit_task_output.text_outputs 提交，引用每份分析和 Lint 结果为 [artifact:ID]。",
        }
        if contract.assigned_agent == run.primary_agent:
            payload["user_request"] = run.user_instruction
            payload["clarifications"] = [item["content"] for item in self.repo.decisions(run.run_id)]
            if request.orchestration_mode == "chat":
                payload["conversation"] = json.loads(self.repo.checkpoint(run.run_id)["snapshot"])
        else:
            payload["clarifications"] = [item["content"] for item in self.repo.decisions(run.run_id, contract.task_id)]
        if "real_rtl_lint" in contract.capability_requirements:
            payload["verilator_lint_arguments"] = {
                "files": [request.rtl_path], "top_module": request.lint_top_module or request.module_name,
            }
        if request.orchestration_mode == "eda_project":
            payload["eda_arguments"] = eda_workflow(request, run.primary_agent)[1]
            payload["output_guidance"] = "提交 result。最终报告用 [artifact:ID] 引用每份证据，并照录实际 EDA 状态。"
            if "propose_eda_patch" in contract.capability_requirements:
                project = next(item for item in inputs if item.artifact.artifact_type == "project_manifest")
                files = FrozenProject.model_validate_json(project.content).files
                payload["repair_source"] = files.get(request.eda_repair_path, "")[:65_000]
        return payload

    @staticmethod
    def _artifact_summary(item: ArtifactPayload) -> str | None:
        if item.artifact.artifact_type == "rtl_snapshot":
            return None
        if item.artifact.artifact_type == "project_manifest":
            project = FrozenProject.model_validate_json(item.content)
            return json.dumps({
                "manifest": project.manifest.model_dump(mode="json"), "input_hash": project.input_hash,
                "file_hashes": project.hashes,
            }, ensure_ascii=False)[:16_000]
        return item.content.decode("utf-8")[:16_000]

    def _save_task_manifest(self, run, state, contract, payload, evidence) -> None:
        snapshot = payload.get("conversation", {}).get("_context_manifest", {})
        encoded = self.repo.db.dumps(payload).encode()
        self.repo.db.execute(
            """INSERT INTO context_manifests(
               context_id, session_id, run_id, agent_role, token_budget, estimated_tokens, message_ids_json,
               memory_ids_json, chunk_ids_json, observation_ids_json, sections_json, created_at,
               orchestration_task_id, artifact_ids_json, prompt_hash, context_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 4)""",
            (str(uuid4()), run.session_id, state.run_id, contract.assigned_agent.value,
             self.contexts.token_budget, self.contexts.tokens.count(encoded.decode()),
             self.repo.db.dumps(snapshot.get("message_ids", [])), self.repo.db.dumps(snapshot.get("memory_ids", [])),
             self.repo.db.dumps(snapshot.get("chunk_ids", [])),
             self.repo.db.dumps([item.ref_id for item in evidence if item.kind == "observation"]),
             self.repo.db.dumps({"decision": state.current_iteration}), now().isoformat(), state.task_id,
             self.repo.db.dumps([item.ref_id for item in evidence if item.kind == "artifact"]),
             hashlib.sha256(encoded).hexdigest()),
        )

    def _check_workspace(self, request: ChatRequest) -> None:
        if request.workspace_id:
            workspace = self.actions.workspaces.get(request.workspace_id)
            if workspace is None or workspace.project_id != request.project_id:
                raise ValueError("Workspace 不属于当前项目或不存在。")

    def _check_documents(self, request: ChatRequest) -> None:
        if len(request.document_ids) > 32:
            raise ValueError("Too many pinned document versions")
        for document_id in request.document_ids:
            version = self.contexts.documents.get_version(document_id)
            if version is None or (version.project_id, version.module_name) != (request.project_id, request.module_name):
                raise ValueError("Document version is outside this Run's scope")

    def _wait_for_inputs(self, run: RunView, message: str) -> None:
        with self.repo.db.transaction() as conn:
            self.repo.open_wait(conn, run.run_id, {"kind": "clarification", "message": message})
            self.repo.post_response(conn, run.run_id, message)

    def _finish(self, run: RunView, status: str, message: str, report_id: str | None = None, *, blocked=False, plan_id=None) -> None:
        with self.repo.db.transaction() as conn:
            current = conn.execute("""SELECT r.status,h.replan_trigger_json,p.plan_id FROM runs r
                                      JOIN orchestration_runs h USING(run_id)
                                      LEFT JOIN orchestration_plans p ON p.run_id=r.run_id AND p.status='active'
                                      WHERE r.run_id=?""", (run.run_id,)).fetchone()
            if current["status"] in TERMINAL or (plan_id is not None and current["plan_id"] != plan_id):
                return
            if report_id is not None and (current["replan_trigger_json"] or not conn.execute(
                """SELECT 1 FROM orchestration_artifacts a JOIN orchestration_tasks t USING(task_id)
                   JOIN orchestration_plans p ON p.run_id=t.run_id AND p.status='active'
                   WHERE a.artifact_id=? AND p.plan_id=? AND a.status='verified'
                     AND a.task_id=json_extract(p.plan_json,'$.final_task_id')
                     AND json_extract(a.artifact_json,'$.source_generation')=json_extract(t.state_json,'$.generation')""",
                (report_id, plan_id),
            ).fetchone()):
                return
            states = self.repo.states(run.run_id)
            invalidated = (set(ReplanTrigger.model_validate_json(current["replan_trigger_json"]).required_reruns)
                           if current["replan_trigger_json"] else set())
            for state in states.values():
                if (state.status not in TERMINAL and state.status != "blocked") or state.task_id in invalidated:
                    next_status = "cancelled" if status == "cancelled" else "blocked" if blocked or state.status == "pending" or state.task_id in invalidated else "failed"
                    self.repo.save_state(conn, state.model_copy(update={
                        "status": next_status, "phase": None, "wait_id": None,
                        "failure_reason": message[:8000], "completed_at": now(),
                    }))
                    self.repo.event(conn, run.run_id, f"task_{next_status}", message[:1000], task_id=state.task_id)
            conn.execute("UPDATE orchestration_waits SET status='cancelled' WHERE run_id=? AND status='open'", (run.run_id,))
            self.repo.close_plan(conn, run.run_id)
            result = {"final_artifact_id": report_id} if report_id else {"message": message, "capability_gap": blocked}
            if tool_run_id := self.repo.latest_tool_run_id(run.run_id):
                project_results = self._project_results(run.run_id)
                if project_results:
                    result["eda_tool_runs"] = project_results
                else:
                    result["lint_tool_run_id"] = tool_run_id
            self.repo.update_run(conn, run.run_id, status, result=result)
            self.repo.post_response(conn, run.run_id, message)
            self.repo.event(conn, run.run_id, f"run_{status}", "Run 已结束。" if report_id else message)

    def _project_results(self, run_id: str) -> list[dict]:
        return [
            {"tool_run_id": tool_id, "tool_name": result.tool_name, "status": result.status}
            for tool_id in self.repo.tool_run_ids(run_id)
            if (result := self.runs.tools.eda_result(tool_id)) is not None
        ]

    def _message(self, run: RunView) -> str:
        if run.pending_user_request:
            return run.pending_user_request["message"]
        if artifact_id := run.result.get("final_artifact_id"):
            return self.artifact(run.run_id, artifact_id).content.decode("utf-8")
        return run.result.get("message", "Run 正在处理。")

    def artifact(self, run_id: str, artifact_id: str) -> ArtifactPayload:
        self._run(run_id)
        items = self.repo._artifacts(
            "JOIN orchestration_tasks t ON t.task_id=a.task_id WHERE t.run_id=? AND a.artifact_id=? AND a.status='verified'",
            (run_id, artifact_id),
        )
        if not items:
            raise KeyError(artifact_id)
        self.verifier._check_content(items[0])
        return items[0]

    def _enqueue_summary(self, run: RunView) -> None:
        try:
            self.contexts.summaries.enqueue_turn(
                self.repo.checkpoint(run.run_id)["turn_id"], run.session_id, run.primary_agent.value,
            )
        except Exception:
            logger.exception("Could not enqueue summary for Run %s", run.run_id)
