from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.agents.runtime import runtime
from app.domain.enums import RunType
from app.domain.models import ChatRequest, ChatResponse, TaskIntent, TaskView, ToolSelection
from app.services.run_service import RunService, run_service
from app.tools.orchestration import register_orchestration_tools
from app.tools.registry import ToolRegistry, tool_registry


MAX_PLANNER_STEPS = 6


class OrchestratorService:
    """Single entrypoint for chat and engineering work.

    The model selects one Tool at a time. Deterministic policy code validates
    the selection, executes it, records an Observation, and then either pauses,
    completes, or lets the model plan another step.
    """

    def __init__(
        self,
        runs: RunService = run_service,
        registry: ToolRegistry = tool_registry,
    ) -> None:
        self.runs = runs
        self.registry = registry
        register_orchestration_tools()

    async def handle(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or str(uuid4())
        request_payload = request.model_dump(mode="json")
        tools = self._tool_context()
        intent = TaskIntent.model_validate(
            runtime.enrich_intent(request_payload, [tool["name"] for tool in tools])
        )
        run_id = self.runs.create_orchestration_run(
            session_id, request, intent.model_dump(mode="json")
        )
        observations: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        for step in range(1, MAX_PLANNER_STEPS + 1):
            selection = ToolSelection.model_validate(
                runtime.select_tool(
                    request_payload,
                    intent.model_dump(mode="json"),
                    tools,
                    observations,
                )
            )
            if selection.name not in {tool["name"] for tool in tools}:
                selection = ToolSelection(
                    name="ask_user",
                    arguments={"question": f"编排模型选择了未注册 Tool：{selection.name}。请重新描述目标。"},
                    reason="拒绝未注册 Tool",
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
                "orchestrator",
            )
            outcome = await self._execute(run_id, request, request_payload, selection)
            if outcome.get("observation") is not None:
                observation = {"tool_name": selection.name, **outcome["observation"]}
                observations.append(observation)
                self.runs.append_event(
                    run_id,
                    "observation_recorded",
                    f"已记录 {selection.name} 的 Observation。",
                    "orchestrator",
                )
            self.runs.record_orchestration(
                run_id,
                intent=intent.model_dump(mode="json"),
                tool_calls=tool_calls,
                observations=observations,
            )
            if outcome.get("terminal"):
                return ChatResponse(
                    session_id=session_id,
                    assistant_message=str(outcome["message"]),
                    route="orchestrated",
                    route_reason=selection.reason,
                    task=outcome.get("task"),
                    run=self.runs.get(run_id),
                )

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
    ) -> dict[str, Any]:
        name = selection.name
        arguments = selection.arguments

        if name == "reply_to_user":
            message = str(arguments.get("message") or runtime.chat(request_payload)).strip()
            self.runs.complete_reply(run_id, message)
            return {"terminal": True, "message": message}

        if name == "ask_user":
            message = str(arguments.get("question") or "请补充完成该任务所需的信息。")
            self.runs.pause_for_user(
                run_id,
                pending_type="clarification_required",
                message=message,
                details={"required": arguments.get("required_fields", [])},
            )
            return {"terminal": True, "message": message}

        if name == "consult_architecture_agent":
            result = runtime.architecture_analysis(request_payload)
            return {"terminal": False, "observation": {**result, "summary": result["summary"]}}

        if name == "consult_design_agent":
            result = runtime.design_analysis(request_payload)
            return {"terminal": False, "observation": {**result, "summary": result["summary"]}}

        if name == "consult_verification_agent":
            result = runtime.verification_analysis(request_payload)
            return {"terminal": False, "observation": {**result, "summary": result["summary"]}}

        if name == "create_ds_candidate":
            run, task, message = await self.runs.execute_ds_tool(run_id, request)
            return {"terminal": True, "message": message, "task": task, "run": run}

        if name == "create_rtl_candidate":
            updates = dict(request.tool_arguments)
            updates.update(arguments)
            run, _, message = self.runs.execute_rtl_tool(
                run_id, request.model_copy(update={"tool_arguments": updates})
            )
            return {"terminal": True, "message": message, "run": run}

        if name in {"verilator_lint", "discover_testcases"}:
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

    def _tool_context(self) -> list[dict[str, Any]]:
        context = []
        for definition in self.registry.list():
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
