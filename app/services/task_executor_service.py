"""Serial hierarchical Run driver with durable decision and action checkpoints."""

import asyncio
import hashlib
import json
import logging
import os
import re
from uuid import uuid4

from app.agents.runtime import runtime
from app.domain.enums import AgentRole, Mutability, ToolRunStatus
from app.domain.models import ChatRequest, ChatResponse, RunResumeRequest, RunView, TaskIntent, ToolRunRequest, ToolRunView, ToolSelection
from app.domain.orchestration import (
    TASK_CONTROLS, Artifact, ArtifactPayload, AuthorizedAction, EvidenceRef, LintResult, Observation, ProfessionalRef, ReflectionRecord,
    RunExecutionPolicy, TaskActionContext, TaskActionResult, TaskContract, TaskOutputRef, TaskPlan,
    TaskPlanDraft, TaskState, VerificationResult,
)
from app.models.provider import ModelInvocationError
from app.services.context_builder_service import ContextBuilderService
from app.services.global_planner_service import GlobalPlannerService, InvalidPlan, PlanningPolicy
from app.services.run_service import RunService
from app.services.task_action_service import TaskActionService
from app.services.task_verifier_service import TaskVerifierService
from app.services.token_usage_service import token_usage_tracker
from app.services.tool_selector_service import ToolSelectorService
from app.storage.orchestration_repository import OrchestrationRepository, now
from app.tools.builtin import verilator_available, verilator_enabled


logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}
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
        self.runtime = agent_runtime
        self.planner = planner or GlobalPlannerService(actions.registry)
        self.verifier = TaskVerifierService(runs.tools)
        self._driving: set[str] = set()

    async def handle(self, request: ChatRequest) -> ChatResponse:
        if os.getenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "").lower() not in {"1", "true"}:
            raise HierarchicalModeDisabled("分层编排开发入口尚未启用。")
        if request.orchestration_mode != "rtl_analysis" and (request.run_lint or request.lint_top_module):
            raise ValueError("真实 Lint 仅支持 rtl_analysis 入口。")
        session_id = request.session_id or str(uuid4())
        session = self.repo.sessions.resolve(session_id, request.project_id, request.user_role, request.module_name)
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
        plan = TaskPlan.model_validate_json(checkpoint["plan_json"])
        while True:
            states = self.repo.states(run_id)
            active_id = self.repo.checkpoint(run_id)["active_task_id"]
            if active_id:
                if states[active_id].status == "failed":
                    self._finish(run, "failed", "交付件未通过独立验收。")
                    return
                contract = next(item for item in plan.tasks if item.task_id == active_id)
                if not await self._step(run, request, plan, contract, states[active_id]):
                    return
            elif all(state.status == "completed" for state in states.values()):
                final = self.repo.local_outputs(plan.final_task_id, "verified")
                report = next(item for item in final if item.artifact.artifact_type == "report")
                self.verifier._check_content(report)
                self._finish(run, "completed", report.content.decode("utf-8"), report.artifact.artifact_id)
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
                    self.repo.claim(states[contract.task_id], contract)
                except ValueError:
                    self._finish(run, "failed", "下游任务缺少已验证的必需输入。")
                    return

    async def _plan(self, run: RunView, request: ChatRequest, checkpoint: dict) -> bool:
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
            limits = RunExecutionPolicy(max_plan_tasks=limit)
        except ValueError:
            self._finish(run, "failed", "计划节点上限配置无效。")
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
        self.repo.save_plan(proposal.plan)
        return True

    @staticmethod
    def _validate_rtl_plan(plan: TaskPlanDraft, *, wants_lint: bool = False) -> None:
        by_id = {node.task_id: node for node in plan.tasks}
        final = by_id[plan.final_task_id]
        design = [node for node in plan.tasks if {"read_rtl", "rtl_design_analysis"} <= set(node.capability_requirements)]
        review = [node for node in plan.tasks if "rtl_verification_assessment" in node.capability_requirements]
        if len(plan.tasks) != 3 + int(wants_lint) or len(design) != 1 or len(review) != 1 or len({design[0].task_id, review[0].task_id, final.task_id}) != 3:
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
        if wants_lint:
            lint = next(node for node in plan.tasks if node.task_id not in {design.task_id, review.task_id, final.task_id})
            if (
                lint.assigned_agent != final.assigned_agent or lint.constraints.read_only
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

    async def _step(
        self, run: RunView, request: ChatRequest, plan: TaskPlan, contract: TaskContract, state: TaskState,
    ) -> bool:
        last = self.repo.last_action(state.task_id)
        if state.phase == "verifying":
            return self._verify(run, plan, contract, state, last)
        if last and last["status"] == "done":
            result = json.loads(last["result_json"])
            if result.get("error_code") in {"task_failure_reported", "capability_gap", "interrupted_unknown", "approval_rejected", "approval_target_changed"} or result.get("outcome") == "awaiting_external":
                self._finish(run, "failed", result["summary"], blocked=result.get("error_code") == "capability_gap")
                return False
        if last and last["status"] in {"selecting", "selected", "dispatching"}:
            if last["status"] == "selecting":
                self._record(state, last["action_id"], "decision", "unknown", "模型决策在服务中断前未写入。")
                return True
            if last["status"] == "dispatching" and json.loads(last["selection_json"])["name"] not in REPLAYABLE | {"verilator_lint"}:
                message = "上次工程动作在服务中断后无法确认结果。"
                delegate = self.actions.delegations.reconcile_interrupted(run.run_id, last["action_id"])
                interrupted = TaskActionResult(
                    outcome="failed", summary=message, error_code="interrupted_unknown",
                    professional_refs=(ProfessionalRef(kind="delegation", object_id=delegate),) if delegate else (),
                )
                self._record(state, last["action_id"], json.loads(last["selection_json"])["name"], "unknown", message, result=interrupted)
                self._finish(run, "failed", message)
                return False
            return await self._dispatch(run, request, contract, state, last)
        if state.current_iteration >= contract.execution_policy.max_decisions:
            self._finish(run, "failed", "任务决策额度已耗尽，交付件未通过验收。")
            return False

        state, action_id = self.repo.reserve(state)
        observations = self.repo.observations(state.task_id)
        candidates = self.repo.local_outputs(state.task_id, "staged")
        inputs = self.repo.bound_inputs(state.task_id)
        for item in (*inputs, *candidates):
            self.verifier._check_content(item)
        verification = self.repo.last_verification(state.task_id)
        payload = self._decision_context(run, request, contract, observations, inputs, candidates, verification)
        evidence = (
            *(EvidenceRef(kind="observation", ref_id=item.observation_id) for item in observations),
            *(EvidenceRef(kind="artifact", ref_id=item.artifact.artifact_id) for item in (*inputs, *candidates)),
        )
        self._save_task_manifest(run, state, contract, payload, evidence)
        permitted = self._permitted_tools(contract)
        controls_only = (
            state.current_iteration >= contract.execution_policy.max_decisions
            or state.action_count >= contract.execution_policy.max_action_steps
            or ("real_rtl_lint" in contract.capability_requirements and state.action_count > 0)
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
        return await self._dispatch(run, request, contract, state, self.repo.last_action(state.task_id))

    async def _dispatch(self, run: RunView, request: ChatRequest, contract: TaskContract, state: TaskState, action: dict) -> bool:
        selection = ToolSelection.model_validate_json(action["selection_json"])
        name = selection.name
        replay = action["status"] == "dispatching"
        counted = name not in TASK_CONTROLS
        lint = name == "verilator_lint"
        link = self.runs.tools.frozen_lint_link(action["action_id"]) if lint else None
        if link:
            tool_run = self.runs.tools.reconcile_frozen_lint(link["tool_run_id"])
            if tool_run.status != ToolRunStatus.QUEUED:
                self.repo.mark_external(state, tool_run.run_id)
                return self._collect_lint(state, contract, action["action_id"], tool_run, json.loads(link["target_json"]))
        context = TaskActionContext(
            run_id=state.run_id, generation=state.generation, action_id=action["action_id"], contract=contract,
            primary_agent=run.primary_agent, project_id=run.project_id, module_name=run.module_name,
            workspace_id=request.workspace_id, allowed_rtl_paths=(request.rtl_path,) if request.rtl_path else (),
            permitted_tools=self._permitted_tools(contract),
            lint_top_module=(request.lint_top_module or request.module_name) if lint else None,
            inputs=(*self.repo.bound_inputs(state.task_id), *self.repo.local_outputs(state.task_id, "staged")),
            action_count=state.action_count - int(replay and counted),
        )
        shortlist = [
            tool for tool in self.actions.registry.for_role(contract.assigned_agent, task=True)
            if tool.name in json.loads(action["shortlist_json"])
        ]
        approval = self.repo.approved_action(action["action_id"]) if lint else None
        result = self.actions.authorize_task_action(context, selection, shortlist, approval)
        if lint and approval and not isinstance(result, AuthorizedAction):
            if link:
                self.runs.tools.cancel_queued_lint(link["tool_run_id"])
            message = "已批准的 Lint 目标或环境发生变化，请重新发起 Run。"
            result = TaskActionResult(outcome="failed", summary=message, error_code="approval_target_changed")
        staged = []
        if isinstance(result, AuthorizedAction):
            if not replay:
                state = self.repo.dispatch(state, action["action_id"], counted)
            if lint:
                target, content = self.actions.lint_target(context, selection)
                tool_run = self.runs.tools.start_frozen_lint(
                    action["action_id"], ToolRunRequest(
                        workspace_id=context.workspace_id, role=contract.assigned_agent, tool_name=name,
                        arguments=selection.arguments, user_approved=True,
                    ), target, content,
                )
                self.repo.mark_external(state, tool_run.run_id)
                return self._collect_lint(state, contract, action["action_id"], tool_run, target)

            def stage(key: str, content: bytes, metadata: dict) -> Artifact:
                item = self.repo.staged_payload(state, contract, action["action_id"], key, content, metadata)
                staged.append(item)
                return item.artifact

            result = await self.actions.execute_task_action(context, result, stage)
        if result.outcome == "observed" and staged:
            provenance = result.evidence or tuple(
                EvidenceRef(kind="artifact", ref_id=item.artifact.artifact_id)
                for item in self.repo.bound_inputs(state.task_id)
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
        status = "waiting" if pending else "failed" if result.outcome == "failed" else "succeeded"
        self._record(
            state, action["action_id"], name, status, result.summary, result=result,
            staged=staged if status == "succeeded" else [], pending=pending,
            verify=name == "submit_task_output" and result.outcome == "observed",
        )
        if pending:
            return False
        if result.error_code in {"task_failure_reported", "capability_gap", "approval_target_changed"} or result.outcome == "awaiting_external":
            self._finish(run, "failed", result.summary, blocked=result.error_code == "capability_gap")
            return False
        return True

    @staticmethod
    def _permitted_tools(contract: TaskContract) -> tuple[str, ...]:
        if "real_rtl_lint" in contract.capability_requirements:
            return ("verilator_lint",)
        return READ_ONLY_TOOLS.get(contract.assigned_agent, ())

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
        self.repo.record_outcome(
            state, observation, result.model_dump(mode="json") if result else {}, list(staged), verify=verify, pending=pending,
        )

    def _verify(self, run: RunView, plan: TaskPlan, contract: TaskContract, state: TaskState, action: dict) -> bool:
        result = TaskActionResult.model_validate_json(action["result_json"])
        ids = set(result.candidate_artifact_ids)
        submitted = tuple(item for item in self.repo.local_outputs(state.task_id, "staged") if item.artifact.artifact_id in ids)
        inputs = self.repo.bound_inputs(state.task_id)
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
            self.repo.record_verification(completed, action["action_id"], verification, tuple(ids), global_result=global_result)
            return True
        failure = global_result if global_result is not None and not global_result.passed else verification
        if result.error_code != "lint_failed" and failure.repairable and state.repair_attempts < contract.execution_policy.max_repair_attempts and state.current_iteration < contract.execution_policy.max_decisions:
            repaired = state.model_copy(update={"phase": "repairing", "repair_attempts": state.repair_attempts + 1})
            self.repo.record_verification(repaired, action["action_id"], verification, tuple(ids), global_result=global_result)
            return True
        failed = state.model_copy(update={
            "status": "failed", "phase": None, "completed_at": now(), "failure_reason": "交付件未通过独立验收。",
        })
        self.repo.record_verification(failed, action["action_id"], verification, tuple(ids), global_result=global_result)
        message = result.summary if result.error_code == "lint_failed" else "交付件未通过独立验收，修正额度已耗尽。"
        self._finish(run, "failed", message)
        return False

    def _decision_context(self, run, request, contract, observations, inputs, candidates, verification) -> dict:
        payload = {
            "message": contract.goal,
            "rtl_path": request.rtl_path if "read_rtl" in contract.capability_requirements else None,
            "observations": [item.model_dump(mode="json") for item in observations],
            "artifacts": [{
                **item.artifact.model_dump(mode="json"),
                "text": item.content.decode("utf-8")[:16_000] if item.artifact.artifact_type != "rtl_snapshot" else None,
            } for item in (*inputs, *candidates)],
            "verification_feedback": json.loads(verification["result_json"]) if verification else None,
            "output_guidance": "报告在 submit_task_output.text_outputs 提交，引用每份分析和 Lint 结果为 [artifact:ID]。",
        }
        if contract.assigned_agent == run.primary_agent:
            payload["user_request"] = run.user_instruction
            payload["clarifications"] = [item["content"] for item in self.repo.decisions(run.run_id)]
            if request.orchestration_mode == "chat":
                payload["conversation"] = json.loads(self.repo.checkpoint(run.run_id)["snapshot"])
        if "real_rtl_lint" in contract.capability_requirements:
            payload["verilator_lint_arguments"] = {
                "files": [request.rtl_path], "top_module": request.lint_top_module or request.module_name,
            }
        else:
            payload["clarifications"] = [item["content"] for item in self.repo.decisions(run.run_id, contract.task_id)]
        return payload

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

    def _finish(self, run: RunView, status: str, message: str, report_id: str | None = None, *, blocked=False) -> None:
        states = self.repo.states(run.run_id)
        with self.repo.db.transaction() as conn:
            for state in states.values():
                if state.status not in TERMINAL and state.status != "blocked":
                    next_status = "cancelled" if status == "cancelled" else "blocked" if blocked or state.status == "pending" else "failed"
                    self.repo.save_state(conn, state.model_copy(update={
                        "status": next_status, "phase": None, "wait_id": None,
                        "failure_reason": message[:8000], "completed_at": now(),
                    }))
                    self.repo.event(conn, run.run_id, f"task_{next_status}", message[:1000], task_id=state.task_id)
            conn.execute("UPDATE orchestration_waits SET status='cancelled' WHERE run_id=? AND status='open'", (run.run_id,))
            conn.execute(
                """UPDATE orchestration_runs SET active_task_id=NULL,
                   plan_json=CASE WHEN plan_json IS NULL THEN NULL ELSE json_set(plan_json, '$.status', 'closed') END
                   WHERE run_id=?""", (run.run_id,),
            )
            result = {"final_artifact_id": report_id} if report_id else {"message": message, "capability_gap": blocked}
            if tool_run_id := self.repo.latest_tool_run_id(run.run_id):
                result["lint_tool_run_id"] = tool_run_id
            self.repo.update_run(conn, run.run_id, status, result=result)
            self.repo.post_response(conn, run.run_id, message)
            self.repo.event(conn, run.run_id, f"run_{status}", "Run 已结束。" if report_id else message)

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
