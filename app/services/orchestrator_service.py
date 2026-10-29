"""统一对话入口的确定性控制平面。

虽然历史名称仍叫 ``OrchestratorService``，但 v0.5 中它不是一个会代替设计/验证
人员作专业决定的“超级 Agent”。它更像操作系统内核：解析当前主 Agent、保存 Run、
控制规划循环、执行权限复核，并把 Tool/Delegate 结果写回 Observation。

一次请求的核心路径：
Session 解析角色 → Intent → Tool 权限过滤 → 轻量 Selector → 主 Agent Tool Call
→ 后端复核 → Tool/Delegate → Observation → 完成或重新规划。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.agents.runtime import runtime
from app.domain.enums import AgentRole, DelegationStatus, RunType
from app.domain.models import ChatRequest, ChatResponse, TaskIntent, TaskView, ToolSelection
from app.services.run_service import RunService, run_service
from app.services.delegation_service import DelegationService, delegation_service
from app.services.session_service import SessionService, session_service
from app.services.tool_selector_service import ToolSelectorService, tool_selector_service
from app.tools.orchestration import register_orchestration_tools
from app.tools.registry import ToolRegistry, tool_registry


MAX_PLANNER_STEPS = 6


class OrchestratorService:
    """普通聊天和工程任务共用的单一控制入口。

    模型每轮只能选择一个 Tool；确定性代码验证选择、执行动作并记录 Observation。
    返回值可能是完成、等待用户，也可能带着 Observation 进入下一轮规划。
    """

    def __init__(
        self,
        runs: RunService = run_service,
        registry: ToolRegistry = tool_registry,
        sessions: SessionService = session_service,
        selector: ToolSelectorService = tool_selector_service,
        delegations: DelegationService = delegation_service,
    ) -> None:
        self.runs = runs
        self.registry = registry
        self.sessions = sessions
        self.selector = selector
        self.delegations = delegations
        register_orchestration_tools()

    async def handle(self, request: ChatRequest) -> ChatResponse:
        """处理一条用户消息，并返回统一 ``ChatResponse``。

        这个方法较长，是因为它清楚展示了顶层状态机。具体专业动作被拆到
        ``RunService``、``DelegationService`` 和各 Tool Handler 中，避免这里
        同时承担 DS、RTL、EDA 等业务细节。
        """
        # 新对话由后端生成 session_id；已有对话必须沿用前端传入的稳定 ID，
        # 这样角色、Run 和后续恢复才能关联到同一会话。
        session_id = request.session_id or str(uuid4())
        request_payload = request.model_dump(mode="json")

        # 读取切换前状态，用来判断是否需要隔离旧角色的私有消息历史。
        previous_conversation = self.sessions.get(session_id)
        role_changed = bool(
            previous_conversation
            and request.user_role
            and previous_conversation.user_role != request.user_role
        )
        conversation = self.sessions.resolve(
            session_id, request.project_id, request.user_role
        )
        primary_agent = conversation.primary_agent
        if role_changed:
            # 只丢弃角色私有聊天历史。request_payload 中的文档、Workspace 等
            # 共享工程上下文仍然保留，因此角色切换不会破坏项目资产。
            request_payload["history"] = []

        # Intent Enricher 也只能看到当前角色有权使用的 Tool 名称，避免它在最早阶段
        # 就建议一个本角色永远无法执行的动作。
        authorized = self.registry.for_role(primary_agent)
        intent = TaskIntent.model_validate(
            runtime.enrich_intent(request_payload, [tool.name for tool in authorized])
        )
        # 先创建 Run，再调用 Selector/模型。这样后续任何错误都有父 Run 可追踪。
        run_id = self.runs.create_orchestration_run(
            session_id, request, intent.model_dump(mode="json"), primary_agent
        )
        if role_changed:
            self.runs.append_event(
                run_id,
                "conversation_role_changed",
                "会话角色已切换；新主 Agent 不继承旧主 Agent 的私有消息历史。",
                primary_agent.value,
            )
        # observations 会反馈给下一轮主 Agent；tool_calls 是实际选择历史；
        # tool_selections 保存第一阶段 Selector 轨迹；delegation_records 保存专家任务快照。
        observations: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_selections: list[dict[str, Any]] = []
        delegation_records: list[dict[str, Any]] = []

        for step in range(1, MAX_PLANNER_STEPS + 1):
            # 第一阶段：从当前主 Agent 已授权 Tool 中选一个有限候选集。
            shortlisted, selector_trace = self.selector.select(
                primary_agent=primary_agent,
                request=request_payload,
                intent=intent.model_dump(mode="json"),
                observations=observations,
            )
            selector_trace["step"] = step
            tool_selections.append(selector_trace)
            # 第二阶段：只有 shortlist 中的 Tool 才展开成完整 Function Calling Schema。
            tools = self._tool_context(shortlisted)
            self.runs.append_event(
                run_id,
                "tools_shortlisted",
                f"Tool Selector 从 {selector_trace['authorized_count']} 个授权 Tool 中选择："
                + "、".join(selector_trace["selected_tools"]),
                primary_agent.value,
            )
            # 当前 Design/Verification 主 Agent 根据用户目标和已有 Observation
            # 从候选集中选择一个 Tool，并生成结构化参数。
            selection = ToolSelection.model_validate(
                runtime.select_tool(
                    request_payload,
                    intent.model_dump(mode="json"),
                    tools,
                    observations,
                    primary_agent.value,
                )
            )
            if selection.name not in {tool["name"] for tool in tools}:
                # 模型选择候选集外的名称时不尝试“猜它想调用什么”，而是退回安全询问。
                selection = ToolSelection(
                    name="ask_user",
                    arguments={"question": f"编排模型选择了未注册 Tool：{selection.name}。请重新描述目标。"},
                    reason="拒绝未注册 Tool",
                )
            # 第三道权限检查：即使 Selector 和主 Agent 都被错误提示影响，执行前仍以
            # Registry 的 enabled 与 allowed_roles 为最终事实来源。
            registered = self.registry.get(selection.name).definition
            if not registered.enabled or primary_agent not in registered.allowed_roles:
                selection = ToolSelection(
                    name="ask_user",
                    arguments={"question": "当前主 Agent 没有执行所选 Tool 的权限，请调整角色或任务。"},
                    reason="执行前权限复核拒绝了 Tool",
                )
            tool_calls.append(
                {
                    "step": step,
                    "tool_name": selection.name,
                    "arguments": selection.arguments,
                    "reason": selection.reason,
                }
            )
            self.runs.append_event(
                run_id,
                "tool_selected",
                f"Planner 选择 {selection.name}：{selection.reason}",
                primary_agent.value,
            )
            # _execute 返回统一 outcome 字典，顶层循环不需要了解各专业服务内部状态机。
            outcome = await self._execute(
                run_id,
                request,
                request_payload,
                selection,
                primary_agent,
            )
            if outcome.get("observation") is not None:
                # 非终态 Tool（典型是 Delegate）把结果写成 Observation，供下一轮汇总。
                observation = {"tool_name": selection.name, **outcome["observation"]}
                observations.append(observation)
                self.runs.append_event(
                    run_id,
                    "observation_recorded",
                    f"已记录 {selection.name} 的 Observation。",
                    primary_agent.value,
                )
            if outcome.get("delegation") is not None:
                delegation_records.append(outcome["delegation"])
            self.runs.record_orchestration(
                run_id,
                intent=intent.model_dump(mode="json"),
                tool_calls=tool_calls,
                observations=observations,
                tool_selections=tool_selections,
                delegations=delegation_records,
            )
            if outcome.get("terminal"):
                # terminal 表示本轮已经完成或需要用户输入，不再继续消耗模型调用。
                return ChatResponse(
                    session_id=session_id,
                    assistant_message=str(outcome["message"]),
                    route="orchestrated",
                    route_reason=selection.reason,
                    task=outcome.get("task"),
                    run=self.runs.get(run_id),
                )

        # 安全阀：防止模型反复调用分析 Tool、循环委派或为了“闭环”制造虚假进展。
        message = "编排达到最大步骤数，已暂停。请缩小任务范围或补充目标。"
        self.runs.pause_for_user(
            run_id,
            pending_type="planner_step_limit",
            message=message,
        )
        self.runs.record_orchestration(
            run_id,
            intent=intent.model_dump(mode="json"),
            tool_calls=tool_calls,
            observations=observations,
            tool_selections=tool_selections,
            delegations=delegation_records,
        )
        return ChatResponse(
            session_id=session_id,
            assistant_message=message,
            route="orchestrated",
            route_reason="Planner 达到安全步数上限",
            run=self.runs.get(run_id),
        )

    async def _execute(
        self,
        run_id: str,
        request: ChatRequest,
        request_payload: dict[str, Any],
        selection: ToolSelection,
        primary_agent: AgentRole,
    ) -> dict[str, Any]:
        """执行一个已经通过候选集和权限复核的 ToolSelection。

        返回字典的约定：
        - ``terminal``：是否结束顶层规划循环；
        - ``message``：结束时返回用户的消息；
        - ``observation``：非终态结果，交给主 Agent 继续规划；
        - ``delegation``：需要写入 Run 的专家任务快照。
        """
        name = selection.name
        arguments = selection.arguments

        if name == "reply_to_user":
            # 模型可能已经在 Tool 参数中生成最终答复；确定性 Runtime 未提供时再调用 chat。
            message = str(arguments.get("message") or runtime.chat(request_payload)).strip()
            self.runs.complete_reply(run_id, message)
            return {"terminal": True, "message": message}

        if name == "ask_user":
            # ask_user 不代表失败，而是一个可恢复的 waiting_for_user 状态。
            message = str(arguments.get("question") or "请补充完成该任务所需的信息。")
            self.runs.pause_for_user(
                run_id,
                pending_type="clarification_required",
                message=message,
                details={"required": arguments.get("required_fields", [])},
            )
            return {"terminal": True, "message": message}

        expert_by_tool = {
            "consult_architecture_agent": AgentRole.ARCHITECTURE,
            "consult_design_agent": AgentRole.DESIGN,
            "consult_verification_agent": AgentRole.VERIFICATION,
        }
        if name in expert_by_tool:
            # consult_* 并不直接调用专家方法，而是创建可查询、可超时的 DelegationTask。
            expert = expert_by_tool[name]
            delegation = await self.delegations.delegate(
                parent_run_id=run_id,
                from_agent=primary_agent,
                to_agent=expert,
                task_type=str(arguments.get("task_type") or name.removeprefix("consult_")),
                instruction=str(arguments.get("focus") or request.message),
                request_payload=request_payload,
            )
            payload = delegation.model_dump(mode="json")
            self.runs.append_event(
                run_id,
                "delegation_completed" if delegation.status == DelegationStatus.COMPLETED else "delegation_failed",
                f"{primary_agent.value} 委派 {expert.value}：{delegation.status.value}",
                expert.value,
            )
            # 无论专家成功或失败，都作为 Observation 返回原主 Agent；专家不会接管会话。
            return {
                "terminal": False,
                "observation": {
                    "summary": delegation.conclusion or delegation.error or "专家委派未返回结论。",
                    "delegation_task_id": delegation.task_id,
                    "expert": expert.value,
                    "status": delegation.status.value,
                    "evidence": delegation.evidence,
                },
                "delegation": payload,
            }

        if name == "create_ds_candidate":
            # 顶层动态规划不重写成熟 DS 图，而是把它作为中粒度 Tool 复用。
            run, task, message = await self.runs.execute_ds_tool(run_id, request)
            return {"terminal": True, "message": message, "task": task, "run": run}

        if name == "create_rtl_candidate":
            # 模型 Tool 参数覆盖请求中的同名参数，但正式写入仍由 Patch 审批保护。
            updates = dict(request.tool_arguments)
            updates.update(arguments)
            run, _, message = self.runs.execute_rtl_tool(
                run_id, request.model_copy(update={"tool_arguments": updates})
            )
            return {"terminal": True, "message": message, "run": run}

        if name in {"verilator_lint", "discover_testcases"}:
            # 外部工程 Tool 使用独立 ToolRun 状态机，顶层 Run 只保存关联 ID。
            run_type = RunType.LINT if name == "verilator_lint" else RunType.TEST_EXECUTION
            updates = dict(request.tool_arguments)
            updates.update(arguments)
            run, _, message = self.runs.execute_engineering_tool(
                run_id,
                request.model_copy(update={"tool_arguments": updates}),
                run_type,
            )
            return {"terminal": True, "message": message, "run": run}

        if name == "report_capability_gap":
            # 未接入 Adapter 时诚实暂停，不能生成一段看似成功的模拟日志。
            capability = str(arguments.get("capability") or "requested capability")
            run = self.runs.report_capability_gap(run_id, capability)
            return {
                "terminal": True,
                "message": run.pending_user_request["message"] if run.pending_user_request else "能力尚未接入。",
                "run": run,
            }

        message = f"Tool {name} 尚无受控执行 Handler。"
        self.runs.pause_for_user(run_id, pending_type="tool_handler_missing", message=message)
        return {"terminal": True, "message": message}

    def _tool_context(self, definitions=None) -> list[dict[str, Any]]:
        """把内部 Tool Definition 转为主 Agent 可用的 Function Calling 上下文。

        ``definitions`` 正常情况下来自 ToolSelector 的 shortlist。保留 ``None`` 回退
        仅用于兼容内部调用；生产主链不会主动把完整 Catalog 发送给主 Agent。
        """
        context = []
        for definition in definitions or self.registry.list():
            description = " ".join(
                part
                for part in (
                    definition.description,
                    f"适用：{definition.when_to_use}" if definition.when_to_use else "",
                    f"不适用：{definition.when_not_to_use}" if definition.when_not_to_use else "",
                    f"前置条件：{'；'.join(definition.preconditions)}" if definition.preconditions else "",
                    f"副作用：{definition.side_effect}",
                    "需要用户审批。" if definition.requires_user_approval else "",
                )
                if part
            )
            context.append(
                {
                    "name": definition.name,
                    "function_description": description,
                    "input_schema": definition.input_schema,
                    "execution_kind": definition.execution_kind,
                }
            )
        return context


orchestrator_service = OrchestratorService()
