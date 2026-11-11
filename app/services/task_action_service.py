"""Task-scoped authorization and typed dispatch for the first read-only capabilities."""

import asyncio
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Callable

from app.agents.runtime import runtime
from app.domain.enums import DelegationStatus, Mutability
from app.domain.models import DelegationArtifactRef, PatchCreateRequest, ToolDefinitionView, ToolSelection
from app.domain.orchestration import (
    TASK_CONTROLS,
    ActionApproval,
    ActionWaitRequest,
    Artifact,
    ArtifactPayload,
    AuthorizedAction,
    EvidenceRef,
    OutputSpec,
    ProfessionalRef,
    RTLDesignAnalysis,
    RTLSourceSnapshot,
    RTLVerificationAssessment,
    TaskActionContext,
    TaskActionResult,
    TaskOutputRef,
)
from app.models.provider import ModelInvocationError
from app.services.delegation_service import ALLOWED_EXPERTS, DelegationService, delegation_service
from app.services.workspace_service import WorkspaceService, workspace_service
from app.tools.registry import ToolRegistry, tool_registry
from app.tools.builtin import check_lint_source, verilator_identity
from app.tools.adapters.verilator_adapter import VERILATOR_TOOLS, SIMULATION_TOOLS, simulation_environment
from app.tools.eda_catalog import EDA_INSPECT, EDA_TOOLS
from app.tools.eda_inspection import coverage_summary, module_hierarchy, signal_references, signal_trace
from app.tools.project_manifest import FrozenProject, read_project


StageArtifact = Callable[[str, bytes, dict[str, str]], Artifact]


class TaskActionService:
    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        workspaces: WorkspaceService = workspace_service,
        delegations: DelegationService = delegation_service,
        agent_runtime=runtime,
    ) -> None:
        self.registry = registry
        self.workspaces = workspaces
        self.delegations = delegations
        self.runtime = agent_runtime
        self.tools = None
        self.patches = None

    def authorize_task_action(
        self,
        context: TaskActionContext,
        selection: ToolSelection,
        shortlist: list[ToolDefinitionView],
        approval: ActionApproval | None = None,
    ) -> AuthorizedAction | TaskActionResult:
        if selection.name not in {tool.name for tool in shortlist}:
            return self._failure("tool_not_shortlisted", "工具不在当前任务候选列表中。")
        return self._authorize(context, selection, approval)

    def _authorize(
        self, context: TaskActionContext, selection: ToolSelection, approval: ActionApproval | None,
    ) -> AuthorizedAction | TaskActionResult:
        contract = context.contract
        role = contract.assigned_agent
        try:
            registered = self.registry.authorize(selection.name, role, task=True)
        except (KeyError, ValueError, PermissionError):
            return self._failure("tool_unavailable", "当前角色无法使用该工具。")
        definition = registered.definition
        control = selection.name in TASK_CONTROLS
        if definition.mutability not in contract.constraints.allowed_mutability or (
            not control and (
                selection.name not in context.permitted_tools
                or registered.capability not in contract.capability_requirements
            )
        ):
            return self._failure("task_scope_denied", "工具超出任务执行范围。")
        if role != context.primary_agent and (
            role not in ALLOWED_EXPERTS.get(context.primary_agent, set())
            or definition.mutability not in {Mutability.READ, Mutability.ANALYSIS}
        ):
            return self._failure("expert_scope_denied", "专家仅可执行已授权的只读任务。")
        if not control and context.action_count >= contract.execution_policy.max_action_steps:
            return self._failure("action_budget", "当前任务的工具动作额度已用尽。")
        try:
            self.registry.validate_arguments(selection.name, selection.arguments)
            serialized = json.dumps(selection.arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return self._failure("invalid_tool_arguments", "工具参数不符合要求。")
        if {"role", "workspace_id", "user_approved", "status", "approval"} & selection.arguments.keys():
            return self._failure("protected_tool_argument", "工具参数包含后端保留字段。")
        if selection.name in EDA_TOOLS:
            scoped_arguments = dict(selection.arguments)
            if selection.name == "propose_eda_patch":
                scoped_arguments.pop("after_content", None)
            if scoped_arguments != context.eda_arguments.get(selection.name):
                return self._failure("eda_scope_denied", "工程工具参数必须与本次用户请求一致。")
        if context.workspace_id:
            workspace = self.workspaces.get(context.workspace_id)
            if workspace is None or workspace.project_id != context.project_id:
                return self._failure("workspace_scope_denied", "任务无法访问该 Workspace。")
        if selection.name == "read_rtl_source" and (
            context.workspace_id is None or selection.arguments["relative_path"] not in context.allowed_rtl_paths
        ):
            return self._failure("rtl_path_scope_denied", "该文件不在任务的 RTL 读取范围内。")
        if selection.name == "create_rtl_candidate" and not any(
            item.artifact.artifact_type == "confirmed_ds"
            and item.artifact.status == "verified" and item.artifact.run_id == context.run_id
            for item in context.inputs
        ):
            return self._failure("confirmed_ds_required", "生成 RTL 候选需要绑定已确认的 DS。")

        argument_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        identity = {
            "run_id": context.run_id, "task_id": contract.task_id,
            "generation": context.generation, "action_id": context.action_id,
            "argument_hash": argument_hash,
        }
        if definition.requires_user_approval:
            if context.workspace_id is None:
                return self._failure("workspace_required", "该工程工具需要 Workspace。")
            input_hash = None
            if selection.name == "verilator_lint" or selection.name in VERILATOR_TOOLS:
                try:
                    target, _ = (
                        self.lint_target(context, selection) if selection.name == "verilator_lint"
                        else self.eda_target(context, selection)
                    )
                except (KeyError, ValueError) as exc:
                    return self._failure("lint_scope_denied" if selection.name == "verilator_lint" else "eda_scope_denied", str(exc))
                input_hash = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
            expected = ActionApproval(
                **identity, workspace_id=context.workspace_id, tool_name=selection.name, input_hash=input_hash,
            )
            if approval != expected:
                message = "执行该工程工具需要批准当前工具参数和 Workspace。"
                return TaskActionResult(
                    outcome="waiting_for_approval", summary=message,
                    wait=ActionWaitRequest(kind="tool_approval", message=message, approval=expected),
                )
        return AuthorizedAction(
            **identity, selection=selection.model_copy(deep=True), approval=approval,
        )

    def lint_target(self, context: TaskActionContext, selection: ToolSelection) -> tuple[dict, bytes]:
        if context.contract.assigned_agent != context.primary_agent:
            raise ValueError("真实 Lint 只能由主角色的专用任务执行。")
        snapshots = [item for item in context.inputs if item.artifact.artifact_type == "rtl_snapshot"]
        if len(snapshots) != 1:
            raise ValueError("真实 Lint 需要一个已验证的 RTL 快照。")
        item = self._bound_input(context, snapshots[0].artifact.artifact_id, "rtl_snapshot")
        snapshot = self._snapshot(item)
        source = snapshot.relative_path
        if (
            source not in context.allowed_rtl_paths
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:v|sv)", PurePosixPath(source.replace("\\", "/")).name, re.I)
            or context.lint_top_module is None
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", context.lint_top_module)
            or selection.arguments != {"files": [source], "top_module": context.lint_top_module}
        ):
            raise ValueError("Lint 参数必须与计划绑定的文件和顶层模块完全一致。")
        check_lint_source(item.content)
        identity = verilator_identity()
        if identity is None:
            raise ValueError("未找到可用的 Verilator 可执行文件。")
        return {
            "snapshot_ref": snapshot.content_ref, "content_hash": snapshot.content_hash,
            "relative_path": source, "top_module": context.lint_top_module, **identity,
        }, item.content

    def eda_target(self, context: TaskActionContext, selection: ToolSelection) -> tuple[dict, FrozenProject]:
        if context.contract.assigned_agent != context.primary_agent:
            raise ValueError("外部 EDA 进程必须由主角色任务申请。")
        source = self._eda_input(context, "project_manifest")
        project = FrozenProject.model_validate_json(source.content)
        identity = verilator_identity()
        if identity is None:
            raise ValueError("未找到可用的 Verilator。")
        if selection.name in SIMULATION_TOOLS and any(
            name not in project.manifest.testcases for name in selection.arguments["testcases"]
        ):
            raise ValueError("用例未在冻结的工程清单登记。")
        if selection.name == "run_regression" and set(selection.arguments["testcases"]) != project.manifest.testcases.keys():
            raise ValueError("完整回归必须显式选择清单中的全部用例。")
        if selection.name == "run_impacted_tests":
            target_path = source.artifact.metadata.get("patch_path")
            if not target_path:
                raise ValueError("影响分析需要已应用且匹配快照的 Patch。")
            impacted = {
                name for name, case in project.manifest.testcases.items()
                if not case.depends_on or target_path in {*case.depends_on, *case.sources}
            }
            if set(selection.arguments["testcases"]) != impacted:
                raise ValueError("选定用例与清单计算的受影响集合不一致。")
        environment = simulation_environment(identity["executable"]) if selection.name in SIMULATION_TOOLS else None
        return {
            "snapshot_ref": source.artifact.artifact_id, "content_hash": project.input_hash,
            "manifest_path": project.manifest_path, "top_module": project.manifest.top_module,
            "arguments": selection.arguments, "simulation_environment": environment, **identity,
        }, project

    def _eda_input(self, context: TaskActionContext, kind: str) -> ArtifactPayload:
        candidates = [item for item in context.inputs if item.artifact.artifact_type == kind]
        if len(candidates) != 1:
            raise ValueError(f"缺少已绑定的工程输入：{kind}")
        return self._bound_input(context, candidates[0].artifact.artifact_id, kind)

    def _inspect_eda(self, context: TaskActionContext, selection: ToolSelection, stage: StageArtifact) -> TaskActionResult:
        name, arguments = selection.name, selection.arguments
        evidence_id = None
        if name == "get_project_manifest":
            if context.workspace_id is None:
                raise ValueError("工程清单读取需要 Workspace")
            project = read_project(self.workspaces, context.workspace_id, context.project_id, arguments["manifest_path"])
            metadata = {"input_hash": project.input_hash, "manifest_path": project.manifest_path}
            if patch_id := arguments.get("applied_patch_id"):
                if self.patches is None:
                    raise ValueError("Patch evidence store is unavailable")
                metadata.update({
                    "patch_id": patch_id,
                    "patch_path": self.patches.verify_applied_snapshot(patch_id, context.workspace_id, project.files),
                })
            payload = self._stage(
                context, stage, "project_manifest", project.model_dump_json().encode("utf-8"),
                metadata,
            )
            return self._observed(f"已冻结 {len(project.files)} 个工程输入文件。", payload)
        if name in {"get_module_hierarchy", "find_signal_references"}:
            source = self._eda_input(context, "project_manifest")
            project = FrozenProject.model_validate_json(source.content)
            data = module_hierarchy(project) if name == "get_module_hierarchy" else signal_references(project, arguments["signal"])
        elif name == "propose_eda_patch":
            return self._propose_eda_patch(context, arguments, stage)
        else:
            source = self._eda_input(context, "eda_result")
            receipt = json.loads(source.content)
            tool_run_id = receipt["tool_run_id"]
            if self.tools is None:
                raise ValueError("ToolRun evidence store is unavailable")
            result = self.tools.eda_result(tool_run_id)
            if result is None:
                raise ValueError("Bound ToolRun has no EDA result")
            if name == "parse_eda_log":
                data = {"tool_run_id": tool_run_id, "status": result.status, "issues": result.issues[:200]}
            elif name == "get_coverage_summary":
                artifacts = {item["name"]: item for item in result.artifacts}
                cases = [case["testcase"] for case in result.metrics.get("testcases", [])]
                summaries = {}
                for case in cases:
                    filename = f"{case}.coverage.dat"
                    if filename in artifacts:
                        summaries[case] = coverage_summary(self.tools.eda_artifact(tool_run_id, filename))
                data = {
                    "tool_run_id": tool_run_id, "testcases": summaries,
                    "missing_testcases": [case for case in cases if case not in summaries],
                }
            else:
                filename = f"{arguments['testcase']}.vcd"
                artifact = next((item for item in result.artifacts if item["name"] == filename), None)
                data = {"tool_run_id": tool_run_id, "testcase": arguments["testcase"], "available": artifact is not None}
                if artifact is not None:
                    data["waveform"] = artifact
                    if name == "analyze_signal_trace":
                        data["trace"] = signal_trace(
                            self.tools.eda_artifact(tool_run_id, filename), arguments["signal"],
                            arguments.get("clock"), arguments.get("ready"),
                        )
                else:
                    data["reason"] = "该次真实仿真没有可用的 VCD 波形。"
        evidence_id = source.artifact.artifact_id
        payload = self._stage(context, stage, name, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return self._observed(f"已生成 {name} 工程证据。", payload, evidence_id)

    def _propose_eda_patch(self, context: TaskActionContext, arguments: dict, stage: StageArtifact) -> TaskActionResult:
        source = self._eda_input(context, "project_manifest")
        receipt = self._eda_input(context, "eda_result")
        proof = json.loads(receipt.content)
        result = proof["result"]
        target = arguments["target_path"]
        project = FrozenProject.model_validate_json(source.content)
        if result["status"] == "PASS":
            artifact = self._stage(
                context, stage, "propose_eda_patch", b'{"status":"not_required"}',
            )
            return self._observed("真实 EDA 已通过，无需候选 Patch。", artifact, receipt.artifact.artifact_id)
        if (
            self.patches is None or context.workspace_id is None or target not in project.manifest.sources
            or target not in project.files or result["status"] != "FAIL"
            or not any(issue.get("file") and issue.get("line") for issue in result["issues"])
        ):
            return self._failure("patch_evidence_missing", "候选 Patch 需要有源码位置的实际失败证据和冻结的目标 RTL。")
        before = project.files[target].replace("\r\n", "\n")
        after = arguments["after_content"]
        if after.replace("\r\n", "\n") == before:
            return self._failure("empty_patch", "候选 Patch 没有改变 RTL。")
        patch = self.patches.create(PatchCreateRequest(
            workspace_id=context.workspace_id, target_path=target, after_content=after,
            reason=context.contract.goal,
            sources=[f"ToolRun:{proof['tool_run_id']}", f"input:{project.input_hash}", f"artifact:{receipt.artifact.artifact_id}"],
        ), expected_before=before)
        artifact = self._stage(context, stage, "propose_eda_patch", patch.model_dump_json().encode("utf-8"))
        return self._observed(f"已生成待审批 Patch {patch.patch_id}。", artifact, receipt.artifact.artifact_id)

    async def execute_task_action(
        self,
        context: TaskActionContext,
        action: AuthorizedAction,
        stage: StageArtifact,
    ) -> TaskActionResult:
        checked = self._authorize(context, action.selection, action.approval)
        if isinstance(checked, TaskActionResult):
            return checked
        if checked != action:
            return self._failure("stale_action", "动作授权与当前任务不匹配。")
        name, args = action.selection.name, action.selection.arguments
        try:
            if name in EDA_INSPECT:
                return await asyncio.to_thread(self._inspect_eda, context, action.selection, stage)
            if name == "read_rtl_source":
                content = await asyncio.to_thread(
                    self.workspaces.read_rtl_source, context.workspace_id, context.project_id, args["relative_path"],
                )
                candidate = self._stage(context, stage, "rtl_snapshot", content, {"relative_path": args["relative_path"]})
                snapshot = self._snapshot(candidate)
                return self._observed(
                    f"已读取 {snapshot.relative_path}，共 {snapshot.line_count} 行。", candidate,
                )
            if name in {"analyze_rtl_design", "assess_rtl_verification"}:
                return await self._analyze(context, name, args, stage)
            if name == "ask_user":
                wait = ActionWaitRequest(
                    kind="clarification", message=args["question"],
                    required_input_keys=tuple(args.get("required_fields", [])),
                )
                return TaskActionResult(outcome="waiting_for_user", summary=wait.message, wait=wait)
            if name == "report_task_failure":
                return TaskActionResult(outcome="failed", summary=args["reason"], error_code="task_failure_reported",
                                        plan_issue=args.get("plan_issue"))
            if name == "submit_task_output":
                return self._submit(context, args, stage)
        except FileNotFoundError:
            return self._failure("rtl_source_missing", "未找到任务指定的 RTL 文件。")
        except ModelInvocationError as exc:
            if exc.code == "MODEL_NOT_CONFIGURED":
                return self._failure("capability_gap", "当前环境缺少真实 RTL 分析模型。")
            return self._failure("analysis_unavailable", "RTL 分析模型当前无法完成请求。")
        except TimeoutError:
            return self._failure("analysis_unavailable", "RTL 分析模型当前无法完成请求。")
        except (PermissionError, ValueError, KeyError, OSError):
            return self._failure("invalid_action_input_or_output", "工具输入或输出未通过检查。")
        return self._failure("capability_gap", "当前任务执行入口尚未接入该工具。")

    def _submit(self, context: TaskActionContext, args: dict, stage: StageArtifact) -> TaskActionResult:
        keys = set(args["output_keys"])
        outputs = {item.key: item for item in context.contract.outputs}
        text_outputs = args.get("text_outputs", {})
        if not keys <= outputs.keys() or not text_outputs.keys() <= keys or any(
            outputs[key].artifact_type != "report" or not outputs[key].media_type.startswith("text/")
            for key in text_outputs
        ):
            return self._failure("invalid_output_key", "提交包含未声明或格式不符的交付件。")
        candidates = {
            item.artifact.output_key: item.artifact.artifact_id for item in context.inputs
            if self._local_candidate(context, item.artifact) and item.artifact.output_key in keys
        }
        for key, text in text_outputs.items():
            if context.eda_arguments:
                entries = []
                for item in context.inputs:
                    if item.artifact.artifact_type == "propose_eda_patch":
                        patch = json.loads(item.content)
                        if patch.get("patch_id"):
                            entries.append(
                                f"- 待审核 Patch {patch['patch_id']} · {patch['target_path']} · [artifact:{item.artifact.artifact_id}]"
                            )
                    if item.artifact.artifact_type == "project_manifest" and item.artifact.metadata.get("patch_id"):
                        entries.append(
                            f"- 已应用 Patch {item.artifact.metadata['patch_id']} · "
                            f"{item.artifact.metadata['patch_path']} · [artifact:{item.artifact.artifact_id}]"
                        )
                    if item.artifact.artifact_type == "eda_result" and item.artifact.status == "verified":
                        receipt = json.loads(item.content)
                        result = receipt["result"]
                        entries.append(
                            f"- {result['tool_name']} **{result['status']}** · {result['tool_version']} · "
                            f"ToolRun {receipt['tool_run_id']} · SHA-256 {result['input_hash']} · "
                            f"[artifact:{item.artifact.artifact_id}]"
                        )
                if entries:
                    text += "\n\n## 实际 EDA 执行记录\n\n" + "\n".join(entries)
            candidate = self._stage_output(context, stage, outputs[key], text.encode("utf-8"), {})
            candidates[key] = candidate.artifact.artifact_id
        return TaskActionResult(
            outcome="observed", summary="已收到候选交付件，等待独立验收。",
            candidate_artifact_ids=tuple(candidates[key] for key in args["output_keys"] if key in candidates),
        )

    async def _analyze(
        self, context: TaskActionContext, name: str, args: dict, stage: StageArtifact,
    ) -> TaskActionResult:
        analyze = getattr(self.runtime, name, None)
        if analyze is None:
            return self._failure("capability_gap", "当前环境缺少真实 RTL 分析模型。")
        is_assessment = name == "assess_rtl_verification"
        source = self._bound_input(context, args["snapshot_ref"], "rtl_snapshot", allow_local=not is_assessment)
        snapshot = self._snapshot(source)
        payload = {
            "task_goal": context.contract.goal,
            "snapshot": snapshot.model_dump(mode="json"),
            "source_text": source.content.decode("utf-8"),
        }
        if is_assessment:
            design = self._bound_input(context, args["design_ref"], "design_analysis")
            analysis = RTLDesignAnalysis.model_validate_json(design.content)
            self._check_findings(analysis.findings, snapshot)
            payload["design_analysis"] = analysis.model_dump(mode="json")

        def run_analysis(inputs: dict) -> RTLDesignAnalysis | RTLVerificationAssessment:
            raw = analyze(inputs)
            if is_assessment:
                result = RTLVerificationAssessment.model_validate(raw)
                self._check_findings((*result.impacts, *result.recommended_checks), snapshot)
                return result
            result = RTLDesignAnalysis.model_validate(raw)
            self._check_findings(result.findings, snapshot)
            return result

        links = ()
        if context.primary_agent == context.contract.assigned_agent:
            report = await asyncio.to_thread(run_analysis, payload)
        else:
            report = None

            def expert_analysis(scoped: dict) -> dict:
                nonlocal report
                report = run_analysis({**payload, "shared_memory": scoped["_memory_context"]})
                return {"summary": report.summary}

            delegation = await self.delegations.delegate(
                parent_run_id=context.run_id, from_agent=context.primary_agent,
                to_agent=context.contract.assigned_agent, task_type=name, instruction=context.contract.goal,
                request_payload={"project_id": context.project_id, "module_name": context.module_name},
                analyze=expert_analysis,
                artifact_refs=(DelegationArtifactRef(path=snapshot.relative_path, version=snapshot.content_hash),),
                orchestration_action_id=context.action_id,
            )
            links = (ProfessionalRef(kind="delegation", object_id=delegation.task_id),)
            if delegation.status != DelegationStatus.COMPLETED or report is None:
                return TaskActionResult(
                    outcome="failed", summary="专家未交付有效的 RTL 分析。",
                    error_code="expert_assessment_failed", professional_refs=links,
                )

        output_type = "verification_assessment" if is_assessment else "design_analysis"
        artifact = self._stage(context, stage, output_type, report.model_dump_json().encode("utf-8"))
        return self._observed(report.summary, artifact, source.artifact.artifact_id).model_copy(
            update={"professional_refs": links},
        )

    @staticmethod
    def _check_findings(findings, snapshot: RTLSourceSnapshot) -> None:
        if any(not ref.matches(snapshot) for item in findings for ref in item.evidence):
            raise ValueError("Finding refers to a different snapshot or invalid line range")

    @staticmethod
    def _local_candidate(context: TaskActionContext, artifact: Artifact) -> bool:
        return (
            artifact.run_id == context.run_id and artifact.source_task_id == context.contract.task_id
            and artifact.source_generation == context.generation and artifact.status == "staged"
        )

    def _bound_input(
        self, context: TaskActionContext, reference: str, artifact_type: str, *, allow_local: bool = False,
    ) -> ArtifactPayload:
        for item in context.inputs:
            artifact = item.artifact
            if artifact.artifact_id != reference or artifact.artifact_type != artifact_type:
                continue
            if allow_local and self._local_candidate(context, artifact):
                return item
            if artifact.run_id == context.run_id and artifact.status == "verified" and any(
                isinstance(spec.source, TaskOutputRef)
                and spec.artifact_type == artifact_type
                and spec.source.task_id == artifact.source_task_id
                and spec.source.output_key == artifact.output_key
                and artifact.source_task_id in context.contract.depends_on
                for spec in context.contract.inputs
            ):
                self._check_content(item)
                return item
        raise ValueError("Input is not an accessible artifact binding")

    @staticmethod
    def _check_content(item: ArtifactPayload) -> None:
        digest = hashlib.sha256(item.content).hexdigest()
        if (
            digest != item.artifact.content_hash or digest != item.artifact.content_ref.object_key
            or len(item.content) != item.artifact.content_ref.byte_size
        ):
            raise ValueError("Artifact content does not match its reference")

    def _snapshot(self, item: ArtifactPayload) -> RTLSourceSnapshot:
        self._check_content(item)
        return RTLSourceSnapshot(
            relative_path=item.artifact.metadata["relative_path"], byte_size=len(item.content),
            line_count=len(item.content.decode("utf-8").splitlines()),
            content_hash=item.artifact.content_hash, content_ref=item.artifact.artifact_id,
        )

    def _stage(
        self, context: TaskActionContext, stage: StageArtifact, artifact_type: str, content: bytes,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactPayload:
        outputs = [item for item in context.contract.outputs if item.artifact_type == artifact_type]
        if len(outputs) != 1:
            raise ValueError("The action needs one declared output of this type")
        return self._stage_output(context, stage, outputs[0], content, metadata or {})

    def _stage_output(
        self, context: TaskActionContext, stage: StageArtifact, spec: OutputSpec,
        content: bytes, metadata: dict[str, str],
    ) -> ArtifactPayload:
        artifact = stage(spec.key, content, metadata)
        if (
            not self._local_candidate(context, artifact) or artifact.source_action_id != context.action_id
            or artifact.artifact_type != spec.artifact_type or artifact.output_key != spec.key
            or artifact.content_ref.media_type != spec.media_type
        ):
            raise ValueError("Staged artifact does not match the task output")
        payload = ArtifactPayload(artifact=artifact, content=content)
        self._check_content(payload)
        return payload

    @staticmethod
    def _observed(summary: str, payload: ArtifactPayload, evidence_id: str | None = None) -> TaskActionResult:
        return TaskActionResult(
            outcome="observed", summary=summary, candidate_artifact_ids=(payload.artifact.artifact_id,),
            evidence=(EvidenceRef(kind="artifact", ref_id=evidence_id),) if evidence_id else (),
        )

    @staticmethod
    def _failure(code: str, summary: str) -> TaskActionResult:
        return TaskActionResult(outcome="failed", summary=summary, error_code=code)
