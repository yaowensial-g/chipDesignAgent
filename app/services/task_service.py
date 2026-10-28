from typing import Any
from uuid import uuid4

from langgraph.types import Command

from app.domain.enums import TaskStatus
from app.domain.models import CreateDSTaskRequest, TaskView, UserResolution
from app.agents.runtime import runtime
from app.storage.database import Database, database
from app.workflows.ds_workflow import ds_workflow


class TaskService:
    def __init__(self, db: Database = database) -> None:
        self.db = db
        self._states: dict[str, dict[str, Any]] = {}

    async def create_ds_task(self, request: CreateDSTaskRequest) -> TaskView:
        task_id = str(uuid4())
        initial_state = {
            "task_id": task_id,
            "request": request.model_dump(mode="json"),
            "status": "requested",
            "events": [],
            "quality_warnings": [],
            "issues": [],
            "findings": [],
        }
        result = await ds_workflow.ainvoke(initial_state, self._config(task_id))
        self._states[task_id] = dict(result)
        self._persist(task_id, self._states[task_id])
        return self._to_view(task_id, self._states[task_id])

    def get_task(self, task_id: str) -> TaskView | None:
        state = self._states.get(task_id)
        if state is None:
            state = self._load(task_id)
            if state is None:
                return None
            self._states[task_id] = state
        return self._to_view(task_id, state)

    async def resume_task(
        self, task_id: str, resolution: UserResolution
    ) -> TaskView:
        if task_id not in self._states:
            state = self._load(task_id)
            if state is None:
                raise KeyError(task_id)
            self._states[task_id] = state
        state = self._states[task_id]
        if "__interrupt__" not in state and not state.get("_pending_user_request"):
            raise ValueError("Task is not waiting for user input")
        if "__interrupt__" in state:
            result = await ds_workflow.ainvoke(
                Command(resume=resolution.model_dump(mode="json")),
                self._config(task_id),
            )
            self._states[task_id] = dict(result)
        else:
            self._states[task_id] = self._resume_persisted(state, resolution)
        self._persist(task_id, self._states[task_id])
        return self._to_view(task_id, self._states[task_id])

    @staticmethod
    def _config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}}

    @staticmethod
    def _interrupt_payload(state: dict[str, Any]) -> dict[str, Any] | None:
        if state.get("_pending_user_request"):
            return state["_pending_user_request"]
        interrupts = state.get("__interrupt__")
        if not interrupts:
            return None
        first = interrupts[0]
        return first.value if hasattr(first, "value") else dict(first)

    def _to_view(self, task_id: str, state: dict[str, Any]) -> TaskView:
        request = state["request"]
        pending = self._interrupt_payload(state)
        status = TaskStatus.WAITING_FOR_USER if pending else TaskStatus(state["status"])
        return TaskView(
            task_id=task_id,
            project_id=request["project_id"],
            module_name=request["module_name"],
            status=status,
            issues=state.get("issues", []),
            findings=state.get("findings", []),
            quality_warnings=state.get("quality_warnings", []),
            events=state.get("events", []),
            pending_user_request=pending,
            ds_markdown=state.get("ds_markdown"),
        )

    def _persist(self, task_id: str, state: dict[str, Any]) -> None:
        snapshot = {key: value for key, value in state.items() if key != "__interrupt__"}
        snapshot["_pending_user_request"] = self._interrupt_payload(state)
        from app.services.workspace_service import utc_now

        self.db.execute(
            "INSERT INTO task_snapshots(task_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
            (task_id, self.db.dumps(snapshot), utc_now()),
        )

    def _load(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            "SELECT state_json FROM task_snapshots WHERE task_id = ?", (task_id,)
        )
        return self.db.loads(row["state_json"], {}) if row else None

    @staticmethod
    def _resume_persisted(
        state: dict[str, Any], resolution: UserResolution
    ) -> dict[str, Any]:
        updated = dict(state)
        updated["_pending_user_request"] = None
        updated["user_resolution"] = resolution.model_dump(mode="json")
        events = list(updated.get("events", []))
        events.append(
            {"event_type": "user_resolution", "message": "已收到用户对架构问题的处理意见。", "agent_id": None}
        )
        if resolution.action.value == "cancel":
            updated.update(status="cancelled", events=events)
            return updated

        request = updated["request"]
        ds_markdown = runtime.generate_ds(
            request, updated.get("design_analysis", {}), updated["user_resolution"]
        )
        findings: list[dict[str, Any]] = []
        review_specs = (
            ("spec_reviewer", "规格一致性"),
            ("microarchitecture_reviewer", "微架构"),
            ("verification_reviewer", "可验证性"),
            ("interface_reviewer", "接口一致性"),
        )
        disabled = set(request.get("disabled_agents", []))
        for agent_id, category in review_specs:
            if agent_id not in disabled:
                findings.extend(runtime.review(agent_id, category, request, ds_markdown))
        for index, prompt in enumerate(request.get("custom_reviewers", []), start=1):
            findings.extend(
                runtime.review(
                    f"custom_reviewer_{index}", "自定义视角", request, ds_markdown, prompt
                )
            )
        events.extend(
            [
                {"event_type": "artifact_created", "message": "已生成 DS Markdown 候选。", "agent_id": "design"},
                {"event_type": "workflow_completed", "message": f"DS 已交付，共保留 {len(findings)} 条独立评审意见。", "agent_id": None},
            ]
        )
        updated.update(
            ds_markdown=ds_markdown,
            findings=findings,
            status="delivered",
            events=events,
        )
        return updated


task_service = TaskService()

