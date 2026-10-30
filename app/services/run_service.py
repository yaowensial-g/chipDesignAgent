"""统一 Run 控制面。

Run 表示“用户要求系统完成的一件事”，但不复制所有专业状态：
- DS 的专业状态仍由 Task/LangGraph 保存；
- RTL 修改内容仍由 Patch 保存；
- 外部工具进程仍由 ToolRun 保存。

Run 只负责把这些对象串在同一条用户任务证据链上，并提供统一的
running / waiting / completed / failed / cancelled 状态给前端。
"""

import re
from typing import Any
from uuid import uuid4

from app.domain.enums import AgentRole, RunStatus, RunType, ToolRunStatus
from app.domain.models import (
    ChatRequest,
    CreateDSTaskRequest,
    PatchDecisionRequest,
    RTLCandidateRequest,
    RunEventView,
    RunResumeRequest,
    RunView,
    TaskView,
    ToolRunRequest,
    UserResolution,
)
from app.services.rtl_service import RTLService, rtl_service
from app.services.patch_service import PatchService, patch_service
from app.services.task_service import TaskService, task_service
from app.services.tool_run_service import ToolRunService, tool_run_service
from app.services.workspace_service import WorkspaceService, utc_now, workspace_service
from app.storage.database import Database, database


TOOL_BY_RUN_TYPE = {
    RunType.LINT: "verilator_lint",
    RunType.TEST_EXECUTION: "discover_testcases",
}


class RunService:
    """持久化并协调跨 Workflow、Patch 和 ToolRun 的用户任务。

    Run 是公共控制面记录，DS Task、Patch 和 ToolRun 是专业执行记录。
    两层分开可以避免把不同领域的全部字段塞进一个万能 JSON。
    """

    def __init__(
        self,
        db: Database = database,
        tasks: TaskService = task_service,
        rtl: RTLService = rtl_service,
        patches: PatchService = patch_service,
        tools: ToolRunService = tool_run_service,
        workspaces: WorkspaceService = workspace_service,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self.rtl = rtl
        self.patches = patches
        self.tools = tools
        self.workspaces = workspaces

    def create_orchestration_run(
        self,
        session_id: str,
        request: ChatRequest,
        intent: dict[str, Any],
        primary_agent: AgentRole,
    ) -> str:
        """创建顶层 Run，并记录原始 Intent 与主 Agent 身份。"""
        run_id = self._create(
            session_id,
            request,
            RunType.ORCHESTRATION,
            "统一 Orchestrator 动态选择 Tool",
            primary_agent,
        )
        self._update(run_id, status=RunStatus.RUNNING, result={"intent": intent, "tool_calls": [], "observations": []})
        self.append_event(run_id, "run_created", "已创建统一编排 Run。", "orchestrator")
        self.append_event(run_id, "intent_enriched", "已完成用户意图结构化，原始请求保持不变。", "orchestrator")
        self.append_event(
            run_id,
            "primary_agent_selected",
            f"当前会话由 {primary_agent.value} Agent 作为主 Agent。",
            primary_agent.value,
        )
        return run_id

    def record_orchestration(
        self,
        run_id: str,
        *,
        intent: dict[str, Any],
        tool_calls: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        tool_selections: list[dict[str, Any]] | None = None,
        delegations: list[dict[str, Any]] | None = None,
    ) -> None:
        """把每轮 Planner 轨迹合并到 Run.result。

        使用整体 JSON 快照便于当前本地 MVP 查询。未来若事件量很大，可以把
        Tool Call/Observation 拆成独立表，但公开 ``RunView`` 契约无需改变。
        """
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        result = dict(run.result)
        result["intent"] = intent
        result["tool_calls"] = tool_calls
        result["observations"] = observations
        if tool_selections is not None:
            result["tool_selections"] = tool_selections
        if delegations is not None:
            result["delegations"] = delegations
        self._update(run_id, result=result)

    def complete_reply(self, run_id: str, message: str) -> RunView:
        """结束普通聊天 Run，并保存最终助手消息。"""
        self._set_run_type(run_id, RunType.CHAT)
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        result = dict(run.result)
        result["assistant_message"] = message
        self._update(run_id, status=RunStatus.COMPLETED, pending=None, result=result)
        self.append_event(run_id, "tool_completed", "reply_to_user 已完成。", "orchestrator")
        return self.get(run_id)  # type: ignore[return-value]

    def complete_mock_eda(
        self,
        run_id: str,
        *,
        stage: str,
        message: str,
        arguments: dict[str, Any],
    ) -> RunView:
        """完成一个无外部副作用的 EDA 占位调用。

        结果中同时保存 ``mock=True`` 和 ``real_execution=False``，避免前端、面试演示
        或后续 Agent 把“Function Call 链路成功”误解为“真实 EDA 检查通过”。
        """
        mapping = {
            "compilation": RunType.COMPILATION,
            "simulation": RunType.SIMULATION,
            "lint": RunType.LINT,
            "cdc": RunType.CDC,
            "synthesis": RunType.SYNTHESIS,
            "sta": RunType.STA,
        }
        run_type = mapping.get(stage)
        if run_type is None:
            raise ValueError(f"Unsupported Mock EDA stage: {stage}")
        self._set_run_type(run_id, run_type)
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        result = dict(run.result)
        result["mock_eda"] = {
            "mock": True,
            "real_execution": False,
            "stage": stage,
            "arguments": arguments,
            "message": message,
        }
        self._update(run_id, status=RunStatus.COMPLETED, pending=None, result=result)
        self.append_event(
            run_id,
            "mock_tool_completed",
            f"run_mock_eda 已完成 {stage} 占位调用；未启动外部进程。",
            "orchestrator",
        )
        return self.get(run_id)  # type: ignore[return-value]

    def pause_for_user(
        self,
        run_id: str,
        *,
        pending_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> RunView:
        """把 Run 置为可恢复等待态，而不是把缺少信息当作系统失败。"""
        pending = {"type": pending_type, "message": message, **(details or {})}
        self._update(run_id, status=RunStatus.WAITING_FOR_USER, pending=pending)
        self.append_event(run_id, "input_required", message, "orchestrator")
        return self.get(run_id)  # type: ignore[return-value]

    async def execute_ds_tool(
        self, run_id: str, request: ChatRequest
    ) -> tuple[RunView, TaskView | None, str]:
        """将顶层 Run 切为 DS 类型，并复用既有 DS Workflow。"""
        self._set_run_type(run_id, RunType.DS_DESIGN)
        return await self._start_ds(run_id, request)

    def execute_rtl_tool(
        self, run_id: str, request: ChatRequest
    ) -> tuple[RunView, None, str]:
        """将顶层 Run 切为 RTL 类型，并生成等待审批的 Candidate Patch。"""
        self._set_run_type(run_id, RunType.RTL_IMPLEMENTATION)
        return self._start_rtl(run_id, request)

    def execute_engineering_tool(
        self,
        run_id: str,
        request: ChatRequest,
        run_type: RunType,
    ) -> tuple[RunView, None, str]:
        """启动 Lint/用例发现等工程 Tool，并关联独立 ToolRun。"""
        self._set_run_type(run_id, run_type)
        return self._start_tool(run_id, request, run_type)

    def report_capability_gap(self, run_id: str, capability: str) -> RunView:
        """记录尚未接入的真实能力，避免用虚构执行结果欺骗用户。"""
        mapping = {
            "simulation": RunType.SIMULATION,
            "cdc": RunType.CDC,
            "synthesis": RunType.SYNTHESIS,
            "sta": RunType.STA,
        }
        run_type = mapping.get(capability.lower())
        if run_type:
            self._set_run_type(run_id, run_type)
        pending = {
            "type": "tool_adapter_required",
            "capability": capability,
            "message": f"当前尚未为 {capability} 注册真实 Tool Adapter。",
        }
        self._update(run_id, status=RunStatus.WAITING_FOR_USER, pending=pending)
        self.append_event(run_id, "capability_missing", pending["message"], "orchestrator")
        return self.get(run_id)  # type: ignore[return-value]

    async def _start_ds(
        self, run_id: str, request: ChatRequest
    ) -> tuple[RunView, TaskView | None, str]:
        """校验 DS 前置资料、创建专业 Task，并把状态映射回统一 Run。"""
        # AS 是 DS 的设计基线。缺少时保留 Run 并等待用户，而不是让模型猜架构。
        if not request.documents or not any(document.kind.value == "AS" for document in request.documents):
            pending = {
                "type": "missing_design_baseline",
                "required": ["AS"],
                "message": "DS 任务需要 AS 作为设计基线；FS 和 ICD 为建议输入。",
            }
            self._update(run_id, status=RunStatus.WAITING_FOR_USER, pending=pending)
            self.append_event(run_id, "input_required", pending["message"], "orchestrator")
            return self.get(run_id), None, "已创建 DS Run，但当前缺少 AS。请上传 AS 后重新发起或继续任务。"  # type: ignore[return-value]

        self._update(run_id, status=RunStatus.RUNNING)
        task = await self.tasks.create_ds_task(
            CreateDSTaskRequest(
                project_id=request.project_id,
                module_name=request.module_name,
                documents=request.documents,
                disabled_agents=request.disabled_agents,
                custom_reviewers=request.custom_reviewers,
                max_discussion_rounds=request.max_discussion_rounds,
                instruction=request.message,
            )
        )
        status = self._task_status(task)
        result = {"task": task.model_dump(mode="json")}
        self._update(
            run_id,
            status=status,
            legacy_task_id=task.task_id,
            pending=task.pending_user_request,
            result=result,
        )
        for event in task.events:
            self.append_event(run_id, event.event_type, event.message, event.agent_id)
        if status == RunStatus.WAITING_FOR_USER:
            message = (
                f"已组织 Agent 分析 {request.module_name}，发现 {len(task.issues)} 个"
                "需要真实架构结论的问题。"
            )
        else:
            message = (
                f"{request.module_name} 的 DS 候选已经生成，并完成 "
                f"{len(task.findings)} 个评审视角。"
            )
        return self.get(run_id), task, message  # type: ignore[return-value]

    def _start_rtl(
        self, run_id: str, request: ChatRequest
    ) -> tuple[RunView, None, str]:
        """校验已确认 DS，生成 RTL Candidate，并停在人工 Diff 审批点。"""
        if not request.ds_markdown:
            pending = {
                "type": "missing_confirmed_ds",
                "required": ["confirmed_ds"],
                "message": "RTL 实现需要用户已确认的 DS。",
            }
            self._update(run_id, status=RunStatus.WAITING_FOR_USER, pending=pending)
            self.append_event(run_id, "input_required", pending["message"], "orchestrator")
            return self.get(run_id), None, "已创建 RTL Run，但当前没有已确认 DS。"  # type: ignore[return-value]

        self._update(run_id, status=RunStatus.RUNNING)
        candidate = self.rtl.create_candidate(
            RTLCandidateRequest(
                project_id=request.project_id,
                workspace_id=request.workspace_id,
                module_name=request.module_name,
                target_path=str(request.tool_arguments.get("target_path") or f"rtl/{request.module_name}.sv"),
                ds_markdown=request.ds_markdown,
                documents=request.documents,
                instruction=request.message,
            )
        )
        result = candidate.model_dump(mode="json")
        self._update(
            run_id,
            status=RunStatus.WAITING_FOR_APPROVAL,
            workspace_id=candidate.workspace.workspace_id,
            patch_id=candidate.patch.patch_id,
            pending={
                "type": "patch_approval_required",
                "patch_id": candidate.patch.patch_id,
                "message": "RTL Candidate 已生成，必须由用户 Review Diff 后才能应用。",
            },
            result=result,
        )
        self.append_event(run_id, "patch_ready", "RTL Candidate 与 Diff 已生成。", "design")
        return self.get(run_id), None, "RTL Candidate 已生成，请在应用前 Review Diff。"  # type: ignore[return-value]

    def _start_tool(
        self, run_id: str, request: ChatRequest, run_type: RunType
    ) -> tuple[RunView, None, str]:
        """准备工程 Tool 参数、Workspace 和用户授权。"""
        # 优先使用请求明确指定的 Workspace；未指定时才尝试项目已有 Workspace。
        workspace_id = request.workspace_id
        if not workspace_id:
            workspaces = self.workspaces.list(request.project_id)
            workspace_id = workspaces[0].workspace_id if workspaces else None
        tool_name = TOOL_BY_RUN_TYPE[run_type]
        arguments = dict(request.tool_arguments)
        if run_type == RunType.LINT:
            arguments.setdefault("files", [f"rtl/{request.module_name}.sv"])
            arguments.setdefault("top_module", request.module_name)

        if not workspace_id:
            pending = {
                "type": "workspace_required",
                "message": "工程 Tool 需要包含 RTL/DV 文件的 Workspace。",
            }
            self._update(
                run_id,
                status=RunStatus.WAITING_FOR_USER,
                pending=pending,
                result={"tool_name": tool_name, "arguments": arguments},
            )
            return self.get(run_id), None, "已创建工程 Run，但当前项目还没有 Workspace。"  # type: ignore[return-value]

        # 网页按钮的 user_approved 最明确；自然语言中出现“执行/运行”等词也视为
        # 本轮明确授权。仅讨论 Lint 概念不会满足这个条件。
        approved = request.user_approved or self._is_explicit_execution(request.message)
        if run_type == RunType.LINT and not approved:
            pending = {
                "type": "tool_approval_required",
                "tool_name": tool_name,
                "message": "Lint 会启动本地工程进程，需要用户明确授权。",
            }
            self._update(
                run_id,
                status=RunStatus.WAITING_FOR_APPROVAL,
                workspace_id=workspace_id,
                pending=pending,
                result={"tool_name": tool_name, "arguments": arguments},
            )
            return self.get(run_id), None, "Lint Run 已规划，等待你的明确执行授权。"  # type: ignore[return-value]

        return self._execute_tool(run_id, workspace_id, tool_name, arguments, run_type)

    def _execute_tool(
        self,
        run_id: str,
        workspace_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        run_type: RunType,
    ) -> tuple[RunView, None, str]:
        """创建专业 ToolRun，并把其 ID 关联回顶层 Run。"""
        control = self.db.fetch_one(
            "SELECT primary_agent FROM runs WHERE run_id = ?", (run_id,)
        )
        # ToolRun 也应记录真实调用角色。旧 Run 没有 primary_agent 时才回退 Design。
        tool_role = AgentRole((control or {}).get("primary_agent") or AgentRole.DESIGN.value)
        tool_run = self.tools.start(
            ToolRunRequest(
                workspace_id=workspace_id,
                tool_name=tool_name,
                role=tool_role,
                arguments=arguments,
                user_approved=True,
            )
        )
        self._update(
            run_id,
            status=RunStatus.RUNNING,
            workspace_id=workspace_id,
            tool_run_id=tool_run.run_id,
            pending=None,
            result={"tool_name": tool_name, "arguments": arguments},
        )
        actor = tool_role.value
        self.append_event(run_id, "tool_started", f"已启动 {tool_name}。", actor)
        label = "已有用例发现" if run_type == RunType.TEST_EXECUTION else run_type.value
        return self.get(run_id), None, f"{label} Run 已启动，可在任务面板查看进度。"  # type: ignore[return-value]

    async def resume(self, run_id: str, request: RunResumeRequest) -> RunView:
        """根据 Run 类型恢复 DS、Patch 审批或工程 Tool 授权。

        Resume 只允许作用于 waiting 状态，避免对已完成任务重复应用副作用。
        """
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if request.action == "cancel":
            return await self.cancel(run_id)
        if run.status not in {RunStatus.WAITING_FOR_USER, RunStatus.WAITING_FOR_APPROVAL}:
            raise ValueError("Run is not waiting for user input or approval")

        if run.run_type == RunType.DS_DESIGN and run.legacy_task_id:
            task = await self.tasks.resume_task(
                run.legacy_task_id,
                UserResolution(
                    action=request.action,
                    content=request.content,
                    evidence_reference=request.evidence_reference,
                ),
            )
            self._update(
                run_id,
                status=self._task_status(task),
                pending=task.pending_user_request,
                result={"task": task.model_dump(mode="json")},
            )
            self.append_event(run_id, "run_resumed", "已接收用户结论并恢复 DS Workflow。")
            return self.get(run_id)  # type: ignore[return-value]

        if run.run_type == RunType.RTL_IMPLEMENTATION and run.patch_id:
            if request.action not in {"approve", "reject", "custom"}:
                raise ValueError("RTL Candidate requires approve, reject, or custom")
            patch = self.patches.decide(
                run.patch_id,
                PatchDecisionRequest(action=request.action, comment=request.content),
            )
            result = dict(run.result)
            result["patch"] = patch.model_dump(mode="json")
            next_status = (
                RunStatus.COMPLETED
                if request.action in {"approve", "reject"}
                else RunStatus.WAITING_FOR_USER
            )
            pending = (
                None
                if next_status == RunStatus.COMPLETED
                else {
                    "type": "custom_patch_requested",
                    "message": "请通过新的指令生成满足自定义要求的候选 Patch。",
                }
            )
            self._update(run_id, status=next_status, pending=pending, result=result)
            self.append_event(
                run_id,
                "patch_decision",
                f"用户对 RTL Patch 选择了 {request.action}。",
            )
            return self.get(run_id)  # type: ignore[return-value]

        if run.run_type in TOOL_BY_RUN_TYPE and request.action == "approve":
            workspace_id = request.workspace_id or run.workspace_id
            if not workspace_id:
                raise ValueError("workspace_id is required to execute this Run")
            arguments = dict(run.result.get("arguments", {}))
            arguments.update(request.tool_arguments)
            executed, _, _ = self._execute_tool(
                run_id,
                workspace_id,
                TOOL_BY_RUN_TYPE[run.run_type],
                arguments,
                run.run_type,
            )
            return executed
        raise ValueError("This Run cannot be resumed with the requested action")

    async def cancel(self, run_id: str) -> RunView:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ValueError(f"Run cannot be cancelled from status {run.status.value}")
        if run.tool_run_id and run.status == RunStatus.RUNNING:
            raise ValueError("Running Tool cancellation is not implemented; the Run was not changed")
        result = dict(run.result)
        if run.run_type == RunType.DS_DESIGN and run.legacy_task_id:
            task = await self.tasks.resume_task(
                run.legacy_task_id,
                UserResolution(action="cancel", content=""),
            )
            result["task"] = task.model_dump(mode="json")
        if run.run_type == RunType.RTL_IMPLEMENTATION and run.patch_id:
            patch = self.patches.decide(
                run.patch_id,
                PatchDecisionRequest(action="reject", comment="Run cancelled by user"),
            )
            result["patch"] = patch.model_dump(mode="json")
        self._update(run_id, status=RunStatus.CANCELLED, pending=None, result=result)
        self.append_event(run_id, "run_cancelled", "用户取消了当前 Run。")
        return self.get(run_id)  # type: ignore[return-value]

    def get(self, run_id: str) -> RunView | None:
        """读取 Run；若关联 ToolRun 已结束，先同步其终态再返回。"""
        row = self.db.fetch_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not row:
            return None
        if row.get("tool_run_id") and row["status"] == RunStatus.RUNNING.value:
            self._refresh_tool_status(row)
            row = self.db.fetch_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        events = self.db.fetch_all(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
        return RunView(
            run_id=row["run_id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            module_name=row["module_name"],
            workspace_id=row["workspace_id"],
            run_type=row["run_type"],
            status=row["status"],
            user_instruction=row["user_instruction"],
            route_reason=row["route_reason"],
            primary_agent=row.get("primary_agent"),
            legacy_task_id=row["legacy_task_id"],
            tool_run_id=row["tool_run_id"],
            patch_id=row["patch_id"],
            pending_user_request=self.db.loads(row["pending_user_request_json"], None),
            result=self.db.loads(row["result_json"], {}),
            events=[RunEventView(**event) for event in events],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        agent_id: str | None = None,
    ) -> None:
        """为 Run 追加单调递增事件，供 UI 展示和问题排查。"""
        row = self.db.fetch_one(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM run_events WHERE run_id = ?",
            (run_id,),
        )
        sequence = int(row["sequence"] or 0) + 1
        self.db.execute(
            "INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), run_id, sequence, event_type, message, agent_id, utc_now()),
        )

    def _create(
        self,
        session_id: str,
        request: ChatRequest,
        run_type: RunType,
        reason: str,
        primary_agent: AgentRole | None = None,
    ) -> str:
        """插入最小 Run 记录；后续状态变化统一通过 ``_update``。"""
        run_id = str(uuid4())
        now = utc_now()
        self.db.execute(
            """INSERT INTO runs (
            run_id, session_id, project_id, module_name, workspace_id, run_type,
            status, user_instruction, route_reason, primary_agent, legacy_task_id,
            tool_run_id, patch_id, pending_user_request_json, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, '{}', ?, ?)""",
            (
                run_id,
                session_id,
                request.project_id,
                request.module_name,
                request.workspace_id,
                run_type.value,
                RunStatus.PLANNED.value,
                request.message,
                reason,
                primary_agent.value if primary_agent else None,
                now,
                now,
            ),
        )
        return run_id

    def _update(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        workspace_id: str | None = None,
        legacy_task_id: str | None = None,
        tool_run_id: str | None = None,
        patch_id: str | None = None,
        pending: dict[str, Any] | None | object = ...,
        result: dict[str, Any] | None = None,
    ) -> None:
        """局部更新 Run，同时保留调用方没有传入的字段。

        ``pending=...``（省略号）表示“不修改等待项”，``pending=None`` 才表示
        “明确清空等待项”。这个区别对 Resume/Complete 非常重要。
        """
        row = self.db.fetch_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not row:
            raise KeyError(run_id)
        pending_json = (
            row["pending_user_request_json"]
            if pending is ...
            else self.db.dumps(pending) if pending is not None else None
        )
        self.db.execute(
            """UPDATE runs SET status = ?, workspace_id = ?, legacy_task_id = ?,
            tool_run_id = ?, patch_id = ?, pending_user_request_json = ?, result_json = ?,
            updated_at = ? WHERE run_id = ?""",
            (
                status.value if status else row["status"],
                workspace_id if workspace_id is not None else row["workspace_id"],
                legacy_task_id if legacy_task_id is not None else row["legacy_task_id"],
                tool_run_id if tool_run_id is not None else row["tool_run_id"],
                patch_id if patch_id is not None else row["patch_id"],
                pending_json,
                self.db.dumps(result) if result is not None else row["result_json"],
                utc_now(),
                run_id,
            ),
        )

    def _set_run_type(self, run_id: str, run_type: RunType) -> None:
        self.db.execute(
            "UPDATE runs SET run_type = ?, updated_at = ? WHERE run_id = ?",
            (run_type.value, utc_now(), run_id),
        )

    def _refresh_tool_status(self, row: dict[str, Any]) -> None:
        """把专业 ToolRun 的终态映射到用户可见的统一 Run。"""
        tool_run = self.tools.get(row["tool_run_id"])
        if tool_run is None:
            self._update(row["run_id"], status=RunStatus.FAILED)
            return
        mapping = {
            ToolRunStatus.COMPLETED: RunStatus.COMPLETED,
            ToolRunStatus.FAILED: RunStatus.FAILED,
            ToolRunStatus.TIMED_OUT: RunStatus.FAILED,
            ToolRunStatus.CANCELLED: RunStatus.CANCELLED,
        }
        status = mapping.get(tool_run.status)
        if status:
            result = self.db.loads(row["result_json"], {})
            result["tool_run"] = tool_run.model_dump(mode="json")
            self._update(row["run_id"], status=status, result=result)
            self.append_event(
                row["run_id"],
                "tool_finished",
                f"{tool_run.tool_name} 结束，状态为 {tool_run.status.value}。",
                row.get("primary_agent") or AgentRole.DESIGN.value,
            )

    @staticmethod
    def _task_status(task: TaskView) -> RunStatus:
        """把 DS Task 的细粒度状态折叠为统一 RunStatus。"""
        if task.status.value == "waiting_for_user":
            return RunStatus.WAITING_FOR_USER
        if task.status.value in {"delivered", "accepted"}:
            return RunStatus.COMPLETED
        if task.status.value == "cancelled":
            return RunStatus.CANCELLED
        if task.status.value == "failed":
            return RunStatus.FAILED
        return RunStatus.RUNNING

    @staticmethod
    def _is_explicit_execution(message: str) -> bool:
        compact = re.sub(r"\s+", "", message.lower())
        return bool(re.search(r"执行|运行|开始|跑一下|run", compact))


run_service = RunService()
