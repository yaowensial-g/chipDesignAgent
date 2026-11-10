from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain.models import ChatRequest, RunResumeRequest, ToolSelection, WorkspaceCreateRequest
from app.domain.orchestration import ReflectionResult
from app.main import app
from app.services.context_builder_service import ContextBuilderService
from app.services.conversation_summary_service import ConversationSummaryService
from app.services.delegation_service import DelegationService
from app.services.document_service import DocumentService
from app.services.global_planner_service import GlobalPlannerService
from app.services.memory_service import MemoryService
from app.services.orchestrator_service import orchestrator_service
from app.services.run_service import RunService
from app.services.session_service import SessionService
from app.services.task_action_service import TaskActionService
from app.services.task_executor_service import TaskExecutorService
from app.services.tool_selector_service import ToolSelectorService
from app.services.tool_run_service import ToolRunService
from app.storage.database import Database
from app.storage.orchestration_repository import OrchestrationRepository

from test_task_contracts import task_node, upstream


UART = Path(__file__).parent / "fixtures" / "uart.sv"


def rtl_plan(_):
    design = task_node("design", "design", ("read_rtl", "rtl_design_analysis"), ("rtl_snapshot", "design_analysis"))
    design["outputs"][0]["media_type"] = "text/plain"
    review = task_node(
        "verify", "verification", ("rtl_verification_assessment",), ("verification_assessment",), ("design",),
        (upstream("design", "rtl_snapshot"), upstream("design", "design_analysis")),
    )
    report = task_node(
        "report", "design", ("compose_report",), ("report",), ("verify", "design"),
        (upstream("verify", "verification_assessment"), upstream("design", "design_analysis")),
    )
    report["outputs"][0]["media_type"] = "text/markdown"
    return {
        "goal": "Review UART", "tasks": [report, review, design], "final_task_id": "report",
        "success_criteria": report["acceptance_criteria"],
    }


class ScriptedRuntime:
    def __init__(self, *, exhaust=False, clarify=False):
        self.exhaust = exhaust
        self.clarify = clarify
        self.calls = []

    @staticmethod
    def enrich_intent(payload, _):
        return {"raw_request": payload["message"], "goal": payload["message"]}

    def reflect_and_select(self, contract, context, shortlist, evidence):
        references = {item["artifact_type"]: item["artifact_id"] for item in context["artifacts"]}
        if self.clarify and not context["clarifications"]:
            name, arguments = "ask_user", {"question": "请补充验收关注点。"}
        elif "read_rtl" in contract.capability_requirements:
            reads = sum(item["tool_name"] == "read_rtl_source" for item in context["observations"])
            if not reads or (self.exhaust and reads < 8):
                name, arguments = "read_rtl_source", {"relative_path": context["rtl_path"]}
            elif "design_analysis" not in references and not self.exhaust:
                name, arguments = "analyze_rtl_design", {"snapshot_ref": references["rtl_snapshot"]}
            else:
                name, arguments = "submit_task_output", {"output_keys": ["rtl_snapshot", "design_analysis"]}
        elif "rtl_verification_assessment" in contract.capability_requirements:
            assert "user_request" not in context and "conversation" not in context
            if "verification_assessment" not in references:
                name, arguments = "assess_rtl_verification", {
                    "snapshot_ref": references["rtl_snapshot"], "design_ref": references["design_analysis"],
                }
            else:
                name, arguments = "submit_task_output", {"output_keys": ["verification_assessment"]}
        else:
            text = "UART review\n" + "\n".join(f"[artifact:{ref}]" for ref in references.values())
            name = "submit_task_output"
            key = contract.outputs[0].key
            arguments = {"output_keys": [key], "text_outputs": {key: text}}
        assert name in {tool.name for tool in shortlist}
        decision = {"submit_task_output": "submit", "ask_user": "ask_user"}.get(name, "continue")
        return ReflectionResult(
            progress="in_progress", progress_summary="Test decision", satisfied_criteria=(),
            unmet_criteria=tuple(item.criterion_id for item in contract.acceptance_criteria),
            issues=(), decision=decision, reason="Use fixed test workflow", evidence=evidence,
        ), ToolSelection(name=name, arguments=arguments)

    def analyze_rtl_design(self, payload):
        self.calls.append(("design", payload))
        return {"summary": "UART reset analysis", "findings": [self._finding(payload)]}

    def assess_rtl_verification(self, payload):
        self.calls.append(("verification", payload))
        return {
            "summary": "UART verification", "impacts": [self._finding(payload)],
            "recommended_checks": [self._finding(payload)],
        }

    @staticmethod
    def _finding(payload):
        snapshot = payload["snapshot"]
        assert payload["source_text"] == UART.read_bytes().decode()
        return {
            "statement": "The reset path sets the UART output idle.",
            "evidence": [{
                "snapshot_ref": snapshot["content_ref"], "content_hash": snapshot["content_hash"],
                "start_line": 12, "end_line": 18,
            }],
        }


@pytest.fixture
def executor_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "true")

    def build(agent):
        db = Database(tmp_path / "hierarchical.db")
        sessions = SessionService(db)
        memories = MemoryService(db)
        summaries = ConversationSummaryService(sessions)
        monkeypatch.setattr(summaries, "enqueue_turn", lambda *args: None)
        from app.services.workspace_service import WorkspaceService

        workspaces = WorkspaceService(db, tmp_path / "workspaces")
        runs = RunService(db=db, workspaces=workspaces, tools=ToolRunService(db, workspaces=workspaces))
        contexts = ContextBuilderService(db, sessions, DocumentService(db), memories, summaries)
        delegations = DelegationService(db, memories, SimpleNamespace(extract_from_event=lambda **kwargs: None))
        actions = TaskActionService(workspaces=workspaces, delegations=delegations, agent_runtime=agent)
        return TaskExecutorService(
            runs, OrchestrationRepository(db, sessions), contexts, ToolSelectorService(), actions,
            planner=GlobalPlannerService(propose=rtl_plan), agent_runtime=agent,
        )

    return build


def request_for(executor, *, with_path=True):
    workspace = executor.actions.workspaces.create(WorkspaceCreateRequest(project_id="chip"))
    executor.actions.workspaces.resolve_path(workspace.workspace_id, "rtl/uart.sv").write_bytes(UART.read_bytes())
    return ChatRequest(
        project_id="chip", module_name="uart", message="Review the UART RTL",
        orchestration_mode="rtl_analysis", workspace_id=workspace.workspace_id,
        rtl_path="rtl/uart.sv" if with_path else None,
    )


@pytest.mark.asyncio
async def test_hierarchical_uart_verifies_before_scheduling_downstream(executor_factory):
    agent = ScriptedRuntime()
    executor = executor_factory(agent)
    request = request_for(executor)
    response = await executor.handle(request)
    run = response.run
    assert run.status.value == "completed" and run.engine_version == "hierarchical"
    assert "UART review" in response.assistant_message and set(run.result) == {"final_artifact_id"}
    assert [name for name, _ in agent.calls] == ["design", "verification"]
    assert agent.calls[0][1]["snapshot"] == agent.calls[1][1]["snapshot"]
    plan = executor.repo.checkpoint(run.run_id)["plan_json"]
    assert '"status":"closed"' in plan
    assert [event.event_type for event in run.events].count("verification_passed") == 3
    assert [event.message for event in run.events if event.event_type == "task_started"] == ["design", "verify", "report"]
    for state in executor.repo.states(run.run_id).values():
        assert state.status == "completed" and state.artifact_ids
        assert executor.repo.reflections(state.task_id)[-1].result.decision == "submit"
        assert executor.repo.verifications(state.task_id)[0]["result"]["passed"]
    manifest = executor.contexts.list_for_run(run.run_id)
    assert any(item.orchestration_task_id and item.artifact_ids for item in manifest)
    assert executor.artifact(run.run_id, run.result["final_artifact_id"]).content.startswith(b"UART review")


@pytest.mark.asyncio
async def test_resume_wait_after_reopen_and_switch_disabled(executor_factory, monkeypatch):
    first = executor_factory(ScriptedRuntime(clarify=True))
    request = request_for(first)
    initial = (await first.handle(request)).run
    assert initial.status.value == "waiting_for_user"
    wait = initial.pending_user_request
    task_id = wait["task_id"]
    assert first.repo.states(initial.run_id)[task_id].current_iteration == 1

    restarted = executor_factory(ScriptedRuntime())
    monkeypatch.delenv("CHIP_AGENT_HIERARCHICAL_ENABLED")
    decision = RunResumeRequest(
        action="provide_resolution", content="重点检查复位行为", wait_id=wait["wait_id"], client_request_id="answer-1",
    )
    resumed = await restarted.resume(initial.run_id, decision)
    assert resumed.status.value == "completed"
    repeated = await restarted.resume(initial.run_id, decision)
    assert repeated.status == resumed.status
    assert len(restarted.repo.decisions(initial.run_id)) == 1
    assert restarted.repo.states(initial.run_id)[task_id].current_iteration == 4
    with pytest.raises(ValueError, match="already used"):
        await restarted.resume(initial.run_id, decision.model_copy(update={"content": "different"}))


@pytest.mark.asyncio
async def test_initial_missing_rtl_path_is_a_recoverable_wait(executor_factory):
    executor = executor_factory(ScriptedRuntime())
    run = (await executor.handle(request_for(executor, with_path=False))).run
    assert run.status.value == "waiting_for_user" and not executor.repo.states(run.run_id)
    resumed = await executor.resume(run.run_id, RunResumeRequest(
        action="provide_resolution", wait_id=run.pending_user_request["wait_id"], rtl_path="rtl/uart.sv",
    ))
    assert resumed.status.value == "completed"


@pytest.mark.asyncio
async def test_repair_budget_never_resets_action_budget_and_blocks_downstream(executor_factory):
    agent = ScriptedRuntime(exhaust=True)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and not agent.calls
    states = executor.repo.states(run.run_id)
    failed = next(state for state in states.values() if state.status == "failed")
    assert failed.action_count == 8 and failed.repair_attempts == 3
    assert len(executor.repo.verifications(failed.task_id)) == 4
    assert all(state.status in {"failed", "blocked"} for state in states.values())
    assert not executor.repo.local_outputs(failed.task_id, "verified")
    candidate = executor.repo.local_outputs(failed.task_id, "staged")[0]
    with pytest.raises(KeyError):
        executor.artifact(run.run_id, candidate.artifact.artifact_id)


class SimulatedRestart(BaseException):
    pass


@pytest.mark.asyncio
async def test_read_checkpoint_replays_once_without_recounting(executor_factory, monkeypatch):
    original = executor_factory(ScriptedRuntime())
    request = request_for(original)

    def interrupt(*args):
        raise SimulatedRestart

    monkeypatch.setattr(original.actions.workspaces, "read_rtl_source", interrupt)
    with pytest.raises(SimulatedRestart):
        await original.handle(request)
    run_id, = original.repo.recoverable()
    active = original.repo.checkpoint(run_id)["active_task_id"]
    assert original.repo.last_action(active)["status"] == "dispatching"
    assert original.repo.states(run_id)[active].action_count == 1
    restarted = executor_factory(ScriptedRuntime())
    await restarted.recover()
    assert restarted.runs.get(run_id).status.value == "completed"
    assert restarted.repo.states(run_id)[active].action_count == 2
    assert [item.tool_name for item in restarted.repo.observations(active)].count("read_rtl_source") == 1


@pytest.mark.asyncio
async def test_unknown_expert_action_is_reconciled_without_reexecution(executor_factory, monkeypatch):
    first_agent = ScriptedRuntime()
    executor = executor_factory(first_agent)
    request = request_for(executor)

    def interrupt(_):
        raise SimulatedRestart

    monkeypatch.setattr(first_agent, "assess_rtl_verification", interrupt)
    with pytest.raises(SimulatedRestart):
        await executor.handle(request)
    run_id, = executor.repo.recoverable()
    task_id = executor.repo.checkpoint(run_id)["active_task_id"]
    restarted_agent = ScriptedRuntime()
    restarted = executor_factory(restarted_agent)
    await restarted.recover()
    assert restarted.runs.get(run_id).status.value == "failed"
    assert not restarted_agent.calls
    delegate, = restarted.actions.delegations.list_for_run(run_id)
    assert delegate.status.value == "failed"
    assert restarted.repo.observations(task_id)[-1].error == "interrupted_unknown"


@pytest.mark.asyncio
async def test_hierarchical_chat_uses_one_task_and_pins_document_version(executor_factory):
    from app.domain.models import DocumentIngestRequest

    executor = executor_factory(ScriptedRuntime())
    source = DocumentIngestRequest(
        project_id="chip", module_name="uart", kind="AS", name="UART",
        version="1.0", content="# UART\n旧版复位要求",
    )
    pinned = executor.contexts.documents.ingest(source)
    executor.contexts.documents.ingest(source.model_copy(update={"version": "2.0", "content": "# UART\n新版复位要求"}))
    response = await executor.handle(ChatRequest(
        project_id="chip", module_name="uart", message="Summarize the UART requirements",
        orchestration_mode="chat", document_ids=[pinned.version_id],
    ))
    assert response.run.status.value == "completed"
    assert len(executor.repo.states(response.run.run_id)) == 1
    snapshot = executor.repo.checkpoint(response.run.run_id)["snapshot"].decode()
    assert "旧版复位要求" in snapshot and "新版复位要求" not in snapshot


def test_opt_in_api_switch_and_wait_trace(monkeypatch):
    client = TestClient(app)
    request = {"project_id": "hierarchical-api", "module_name": "uart", "message": "Review RTL", "orchestration_mode": "rtl_analysis"}
    monkeypatch.delenv("CHIP_AGENT_HIERARCHICAL_ENABLED", raising=False)
    assert client.post("/api/v1/chat", json=request).status_code == 409
    monkeypatch.setenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "1")
    response = client.post("/api/v1/chat", json=request)
    assert response.status_code == 200
    run = response.json()["run"]
    run_id = run["run_id"]
    assert run["status"] == "waiting_for_user"
    assert client.get(f"/api/v1/runs/{run_id}/plans").json() == []
    assert client.get(f"/api/v1/runs/{run_id}/tasks").json() == []
    assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"


def test_verified_report_and_trace_available_through_public_api(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "1")
    monkeypatch.setattr(orchestrator_service.hierarchical, "runtime", ScriptedRuntime())
    response = client.post("/api/v1/chat", json={
        "project_id": "hierarchical-report-api", "module_name": "uart",
        "message": "Explain UART", "orchestration_mode": "chat",
    })
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "completed"
    base = f"/api/v1/runs/{run['run_id']}"
    [plan] = client.get(f"{base}/plans").json()
    assert plan["status"] == "closed"
    [task] = client.get(f"{base}/tasks").json()
    assert task["reflections"][-1]["result"]["decision"] == "submit"
    assert len(task["verifications"]) == 2
    artifact_id = run["result"]["final_artifact_id"]
    result = client.get(f"{base}/artifacts/{artifact_id}")
    assert result.status_code == 200 and result.json()["content"].startswith("UART review")


@pytest.mark.asyncio
async def test_explicit_lint_request_reports_capability_gap_without_mock(executor_factory):
    agent = ScriptedRuntime()
    executor = executor_factory(agent)
    request = request_for(executor).model_copy(update={"message": "Run Verilator lint on the UART"})
    response = await executor.handle(request)
    assert response.run.status.value == "failed" and response.run.result["capability_gap"]
    assert not executor.repo.states(response.run.run_id) and not agent.calls
