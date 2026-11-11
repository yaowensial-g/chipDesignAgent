import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain.models import ChatRequest, RunResumeRequest, ToolSelection, WorkspaceCreateRequest
from app.domain.orchestration import ReflectionResult, VerificationResult
from app.main import app
from app.services.context_builder_service import ContextBuilderService
from app.services.conversation_summary_service import ConversationSummaryService
from app.services.delegation_service import DelegationService
from app.services.document_service import DocumentService
from app.services.global_planner_service import GlobalPlannerService
from app.services.memory_service import MemoryService
from app.services.orchestrator_service import orchestrator_service
from app.services.patch_service import PatchService
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
        runs = RunService(
            db=db, workspaces=workspaces, tools=ToolRunService(db, workspaces=workspaces),
            patches=PatchService(workspaces=workspaces, db=db),
        )
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


class ReplanningRuntime(ScriptedRuntime):
    def __init__(self, *, add_prerequisite=False, invalidate=None, broken_delta=False, repair=False, **kwargs):
        super().__init__(**kwargs)
        self.add_prerequisite = add_prerequisite
        self.invalidate = invalidate
        self.broken_delta = broken_delta
        self.repair = repair
        self.proposals = []
        self.retry_feedback = []

    @staticmethod
    def _select(contract, evidence, name, arguments):
        decision = "fail" if name == "report_task_failure" else "submit"
        return ReflectionResult(
            progress="in_progress", progress_summary="Test replan decision", satisfied_criteria=(),
            unmet_criteria=tuple(item.criterion_id for item in contract.acceptance_criteria),
            issues=(), decision=decision, reason="Use persisted evidence", evidence=evidence,
        ), ToolSelection(name=name, arguments=arguments)

    def reflect_and_select(self, contract, context, shortlist, evidence):
        if "previous_attempt_feedback" in context:
            self.retry_feedback.append(context["previous_attempt_feedback"])
        review = "rtl_verification_assessment" in contract.capability_requirements
        if self.repair and review and not context["observations"]:
            return self._select(contract, evidence, "submit_task_output", {"output_keys": ["verification_assessment"]})
        plan_available = "plan_tasks" in context
        prerequisite = self.add_prerequisite and review and len(context.get("plan_tasks", ())) == 3
        invalidating = (self.invalidate and contract.title == "report"
                        and len(self.proposals) == int(self.add_prerequisite))
        if plan_available and (prerequisite or invalidating):
            key = self.invalidate if invalidating else "design_analysis"
            artifact = next(item for item in context["artifacts"] if item["artifact_type"] == key)
            reason = "需要先完成额外检查。" if prerequisite else "上游交付的结论有误。"
            return self._select(contract, evidence, "report_task_failure", {"reason": reason, "plan_issue": {
                "issue_type": "missing_prerequisite" if prerequisite else "artifact_invalid",
                "affected_task_ids": [contract.task_id], "related_artifact_ids": [artifact["artifact_id"]],
                "evidence": [{"kind": "artifact", "ref_id": artifact["artifact_id"]}],
                "reason": reason, "suggested_change": "调整输入和任务依赖。",
            }})
        return super().reflect_and_select(contract, context, shortlist, evidence)

    def propose_plan_delta(self, payload):
        self.proposals.append(payload)
        trigger = payload["trigger"]
        if self.broken_delta:
            return {"operations": [{"op": "update_dependencies", "task_id": trigger["task_id"],
                                    "depends_on": [trigger["task_id"]]}]}
        if trigger["kind"] == "plan_issue":
            task = next(item for item in payload["plan"]["tasks"] if item["task_id"] == trigger["task_id"])
            design = next(item for item in payload["plan"]["tasks"] if "read_rtl" in item["capability_requirements"])
            # This prerequisite consumes the snapshot only. Invalidating design_analysis later must preserve it.
            added = task_node("extra", "design", ("compose_report",), ("report",), (design["task_id"],),
                              (upstream(design["task_id"], "rtl_snapshot"),))
            added["outputs"][0]["media_type"] = "text/markdown"
            return {"operations": [
                {"op": "add_task", "task": added},
                {"op": "update_dependencies", "task_id": task["task_id"], "depends_on": [*task["depends_on"], "extra"]},
            ]}
        requested = trigger["required_reruns"] or [trigger["task_id"]]
        return {"operations": [{"op": "rerun_task", "task_id": key} for key in requested]}


@pytest.fixture
def replan_enabled(monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_REPLANNING_ENABLED", "true")


@pytest.mark.asyncio
async def test_successful_local_repair_uses_no_replan(executor_factory, replan_enabled):
    agent = ReplanningRuntime(repair=True)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "completed", run.result
    review = next(state for state in executor.repo.states(run.run_id).values() if state.repair_attempts)
    assert review.repair_attempts == 1 and review.generation == 1
    assert [entry["result"]["passed"] for entry in executor.repo.verifications(review.task_id)] == [False, True]
    assert not agent.proposals and not executor.repo.replan_history(run.run_id)


@pytest.mark.asyncio
async def test_incremental_prerequisite_and_late_results(executor_factory, replan_enabled, monkeypatch):
    agent = ReplanningRuntime(add_prerequisite=True)
    executor = executor_factory(agent)
    repo = executor.repo
    publish = repo.publish_replan

    def publish_with_late_result(old, new, number, delta, trigger, reruns, removed):
        old_state = repo.states(old.run_id)[trigger.task_id]
        observation = repo.observations(old_state.task_id, old_state.generation)[0]
        publish(old, new, number, delta, trigger, reruns, removed)
        assert repo.plans(old.run_id)[-1].status == "active"
        current = repo.states(old.run_id)[old_state.task_id]
        contract = next(task for task in old.tasks if task.task_id == old_state.task_id)
        staged = repo.staged_payload(old_state, contract, observation.action_id, "verification_assessment", b"late stale output", {})
        assert not repo.record_outcome(old_state, observation, {}, [staged])
        assert not repo.record_verification(old_state, observation.action_id, VerificationResult(passed=True), ())
        assert repo.states(old.run_id)[old_state.task_id] == current
        assert not repo.artifact_history(old_state.task_id, 1)
        assert len(repo.db.fetch_all("SELECT * FROM orchestration_late_results WHERE action_id=?", (observation.action_id,))) == 2

    monkeypatch.setattr(repo, "publish_replan", publish_with_late_result)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "completed", run.result
    old, new = executor.repo.plans(run.run_id)
    assert (old.status, new.status, new.previous_plan_id, new.version) == ("superseded", "closed", old.plan_id, 2)
    assert old.goal == new.goal and old.success_criteria == new.success_criteria
    by_title = {task.title: task for task in new.tasks}
    design, review, extra = (by_title[key].task_id for key in ("design", "verify", "extra"))
    assert extra != "extra" and extra in by_title["verify"].depends_on
    assert [name for name, _ in agent.calls] == ["design", "verification"]
    assert [item["generation"] for item in executor.repo.attempts(run.run_id, design)] == [1]
    assert [item["generation"] for item in executor.repo.attempts(run.run_id, review)] == [1, 2]
    assert executor.repo.action_history(review, 1)[0]["decision_sequence"] == executor.repo.action_history(review, 2)[0]["decision_sequence"] == 1
    assert [record["status"] for record in executor.repo.replan_history(run.run_id)] == ["applied"]

@pytest.mark.asyncio
async def test_confirmed_invalidation_rebinds_only_data_consumers(executor_factory, replan_enabled):
    agent = ReplanningRuntime(add_prerequisite=True, invalidate="design_analysis")
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    pending = run.pending_user_request
    assert run.status.value == "waiting_for_user" and pending["kind"] == "artifact_invalidation", run.result
    repo = executor.repo
    by_title = {task.title: task.task_id for task in repo.plans(run.run_id)[-1].tasks}
    old_a = next(item for item in repo.artifact_history(by_title["design"], 1) if item.artifact_type == "design_analysis")
    old_d = repo.artifact_history(by_title["extra"], 1)[0]
    assert old_a.status == old_d.status == "verified"
    old_b = repo.artifact_history(by_title["verify"], 2)[0]

    # Recover the wait from a new executor before accepting the scoped user confirmation.
    reopened = executor_factory(agent)
    finished = await reopened.resume(run.run_id, RunResumeRequest(action="approve", wait_id=pending["wait_id"]))
    assert finished.status.value == "completed", finished.result
    states = reopened.repo.states(run.run_id)
    assert [states[by_title[name]].generation for name in ("design", "verify", "report", "extra")] == [2, 3, 2, 1]
    assert [name for name, _ in agent.calls] == ["design", "verification", "design", "verification"]
    new_a = next(item for item in repo.artifact_history(by_title["design"], 2) if item.artifact_type == "design_analysis")
    assert new_a.version == 2 and new_a.status == "verified" and new_a.artifact_id != old_a.artifact_id
    assert {item.artifact_id: item.status for item in repo.artifact_history(by_title["design"], 1)}[old_a.artifact_id] == "invalidated"
    assert repo.artifact_history(by_title["verify"], 2)[0].artifact_id == old_b.artifact_id
    assert repo.artifact_history(by_title["verify"], 2)[0].status == "invalidated"
    assert repo.artifact_history(by_title["extra"], 1) == [old_d]
    assert any(row["artifact_id"] == new_a.artifact_id for row in repo.input_bindings(by_title["verify"], 3))
    assert f"[artifact:{new_a.artifact_id}]" in executor.artifact(run.run_id, finished.result["final_artifact_id"]).content.decode()
    assert [item["status"] for item in repo.replan_history(run.run_id)] == ["applied", "applied"]


@pytest.mark.asyncio
async def test_corrupted_verified_blob_triggers_automatic_invalidation(executor_factory, replan_enabled, monkeypatch):
    agent = ReplanningRuntime()
    executor = executor_factory(agent)
    repo = executor.repo
    claim = repo.claim

    def corrupt_once(state, contract, plan_id):
        state = claim(state, contract, plan_id)
        if contract.title == "verify" and state.generation == 1:
            snapshot = next(item for item in repo.bound_inputs(state.task_id) if item.artifact.artifact_type == "rtl_snapshot")
            repo.db.execute("UPDATE orchestration_blobs SET content=? WHERE content_hash=?",
                            (b"corrupted RTL snapshot", snapshot.artifact.content_hash))
        return state

    monkeypatch.setattr(repo, "claim", corrupt_once)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "completed" and not run.pending_user_request, run.result
    assert [name for name, _ in agent.calls] == ["design", "design", "verification"]
    [proposal] = agent.proposals
    assert proposal["trigger"]["kind"] == "artifact_invalid"
    design = next(item for item in repo.plans(run.run_id)[-1].tasks if item.title == "design")
    old = next(item for item in repo.artifact_history(design.task_id, 1) if item.artifact_type == "rtl_snapshot")
    new = next(item for item in repo.artifact_history(design.task_id, 2) if item.artifact_type == "rtl_snapshot")
    assert old.status == "invalidated" and new.status == "verified" and old.content_hash == new.content_hash


@pytest.mark.asyncio
async def test_invalid_deltas_exhaust_persisted_run_budget(executor_factory, replan_enabled):
    agent = ReplanningRuntime(add_prerequisite=True, broken_delta=True)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and "重规划提案额度已耗尽" in run.result["message"]
    [plan] = executor.repo.plans(run.run_id)
    assert plan.version == 1 and plan.status == "closed"
    records = executor.repo.replan_history(run.run_id)
    assert [(item["attempt_no"], item["status"], item["diagnostics"]) for item in records] == [
        (1, "rejected", "dependency_cycle"), (2, "rejected", "dependency_cycle"),
    ]
    assert len(agent.proposals) == 2 and agent.proposals[-1]["validation_feedback"] == "dependency_cycle"


@pytest.mark.asyncio
async def test_exhausted_repair_remains_exhausted_after_rerun(executor_factory, replan_enabled, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_MAX_REPLANS", "1")
    agent = ReplanningRuntime(exhaust=True)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and "重规划提案额度已耗尽" in run.result["message"]
    task_id = next(task.task_id for task in executor.repo.plans(run.run_id)[-1].tasks if task.title == "design")
    first, second = executor.repo.attempts(run.run_id, task_id)
    assert first["repair_attempts"] == second["repair_attempts"] == 3
    assert first["action_count"] == second["action_count"] == 8
    assert first["current_iteration"] == 12 and second["current_iteration"] == 9
    assert len(agent.proposals) == 1 and agent.proposals[0]["trigger"]["kind"] == "repair_exhausted"
    assert "missing:design_analysis" in agent.retry_feedback[0]["failed_criteria"]


@pytest.mark.asyncio
async def test_run_budget_stops_replan_before_model_call(executor_factory, replan_enabled, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_MAX_RUN_ACTIONS", "2")
    agent = ReplanningRuntime(add_prerequisite=True)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and "Run 执行预算已耗尽" in run.result["message"]
    assert not agent.proposals and executor.repo.usage(run.run_id)[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_restart_during_replan_preserves_completed_actions(executor_factory, replan_enabled, monkeypatch, after_commit):
    agent = ReplanningRuntime(add_prerequisite=True)
    first = executor_factory(agent)
    if after_commit:
        publish = first.repo.publish_replan

        def stop(*args, **kwargs):
            publish(*args, **kwargs)
            raise SimulatedRestart()

        monkeypatch.setattr(first.repo, "publish_replan", stop)
    else:
        propose = agent.propose_plan_delta

        def stop(payload):
            propose(payload)
            raise SimulatedRestart()

        monkeypatch.setattr(agent, "propose_plan_delta", stop)
    with pytest.raises(SimulatedRestart):
        await first.handle(request_for(first))
    [run_id] = first.repo.recoverable()
    records = first.repo.replan_history(run_id)
    assert records[0]["status"] == ("applied" if after_commit else "proposing")
    if not after_commit:
        monkeypatch.setattr(agent, "propose_plan_delta", propose)
    second = executor_factory(agent)
    await second.recover()
    run = second.runs.get(run_id)
    assert run.status.value == "completed", run.result
    records = second.repo.replan_history(run_id)
    assert [item["status"] for item in records] == (["applied"] if after_commit else ["interrupted", "applied"])
    assert [name for name, _ in agent.calls] == ["design", "verification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["unauthorized_capability", "original_contract_must_be_preserved"])
async def test_replan_cannot_expand_permissions_or_drop_acceptance(executor_factory, replan_enabled, monkeypatch, rejection):
    agent = ReplanningRuntime(add_prerequisite=True)
    proposal = agent.propose_plan_delta

    def forbidden(payload):
        delta = proposal(payload)
        if rejection == "unauthorized_capability":
            delta["operations"][0]["task"]["capability_requirements"] = ["apply_patch"]
        else:
            task = next(item for item in payload["plan"]["tasks"] if item["task_id"] == payload["trigger"]["task_id"])
            task["acceptance_criteria"][0]["output_keys"] = []
            delta = {"operations": [{"op": "replace_task", "task": task}]}
        return delta

    monkeypatch.setattr(agent, "propose_plan_delta", forbidden)
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and len(executor.repo.plans(run.run_id)) == 1
    assert [item["diagnostics"] for item in executor.repo.replan_history(run.run_id)] == [rejection] * 2


@pytest.mark.asyncio
async def test_rejected_invalidation_does_not_change_verified_inputs(executor_factory, replan_enabled):
    agent = ReplanningRuntime(invalidate="design_analysis")
    executor = executor_factory(agent)
    run = (await executor.handle(request_for(executor))).run
    assert run.pending_user_request["kind"] == "artifact_invalidation"
    rejected = await executor.resume(run.run_id, RunResumeRequest(action="reject", wait_id=run.pending_user_request["wait_id"]))
    assert rejected.status.value == "failed" and "拒绝" in rejected.result["message"]
    assert not agent.proposals
    plan = executor.repo.plans(run.run_id)[0]
    design = next(task for task in plan.tasks if task.title == "design")
    assert all(item.status == "verified" for item in executor.repo.artifact_history(design.task_id, 1))


@pytest.mark.asyncio
async def test_replanning_schema_migrates_an_old_wait_and_bindings(executor_factory, tmp_path, monkeypatch):
    # Capture a durable checkpoint with verified upstream artifacts and a bound consumer waiting for input.
    agent = ScriptedRuntime()
    base_reflect = agent.reflect_and_select

    def ask_after_binding(contract, context, shortlist, evidence):
        if contract.title == "verify" and not context["clarifications"]:
            return ReflectionResult(
                progress="blocked", progress_summary="Need input", satisfied_criteria=(), unmet_criteria=("deliver",),
                issues=(), decision="ask_user", reason="Wait for clarification", evidence=evidence,
            ), ToolSelection(name="ask_user", arguments={"question": "Which reset case matters?"})
        return base_reflect(contract, context, shortlist, evidence)

    monkeypatch.setattr(agent, "reflect_and_select", ask_after_binding)
    first = executor_factory(agent)
    run = (await first.handle(request_for(first))).run
    assert run.status.value == "waiting_for_user"
    plan = first.repo.plans(run.run_id)[0]
    review = next(task.task_id for task in plan.tasks if task.title == "verify")
    design = next(task.task_id for task in plan.tasks if task.title == "design")
    expected_artifacts = first.repo.artifact_history(design, 1)
    expected_bindings = first.repo.input_bindings(review, 1)
    expected_action_ids = [entry["action_id"] for entry in first.repo.action_history(design, 1)]

    # Reconstruct the previous schema in a disposable copy, using its original single-generation ledgers.
    fixture = tmp_path / "old-checkpoint.db"
    with sqlite3.connect(fixture) as previous, first.repo.db.connect() as current:
        current.backup(previous)
        previous.executescript("""
            INSERT INTO orchestration_bindings SELECT task_id,input_key,artifact_id FROM orchestration_attempt_bindings;
            DROP INDEX idx_orchestration_generation_sequence;
            DROP TABLE orchestration_attempt_bindings;
            DROP TABLE orchestration_task_attempts;
            DROP TABLE orchestration_replans;
            DROP TABLE orchestration_late_results;
            ALTER TABLE orchestration_actions DROP COLUMN generation;
            ALTER TABLE orchestration_actions DROP COLUMN generation_sequence;
            ALTER TABLE orchestration_actions DROP COLUMN plan_id;
            ALTER TABLE orchestration_runs DROP COLUMN planning_policy_json;
            ALTER TABLE orchestration_runs DROP COLUMN replan_trigger_json;
            DROP TABLE orchestration_plans;
            UPDATE orchestration_tasks SET state_json=json_remove(state_json,'$.generation');
        """)
    migrated = Database(fixture)
    repo = OrchestrationRepository(migrated, SessionService(migrated))
    assert repo.plans(run.run_id) == [plan]
    assert repo.artifact_history(design, 1) == expected_artifacts
    assert repo.input_bindings(review, 1) == expected_bindings
    assert [entry["action_id"] for entry in repo.action_history(design, 1)] == expected_action_ids
    assert len(repo.attempts(run.run_id, review)) == 1
    monkeypatch.setattr(first, "repo", repo)
    monkeypatch.setattr(first.runs, "db", migrated)
    finished = await first.resume(run.run_id, RunResumeRequest(
        action="provide_resolution", content="Check reset", wait_id=run.pending_user_request["wait_id"],
    ))
    assert finished.status.value == "completed", finished.result
    assert not migrated.fetch_all("PRAGMA foreign_key_check")
    assert OrchestrationRepository(Database(fixture), SessionService(migrated)).plans(run.run_id)[0].status == "closed"


@pytest.mark.asyncio
async def test_hierarchical_uart_verifies_before_scheduling_downstream(executor_factory, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_REPLANNING_ENABLED", "1")
    agent = ScriptedRuntime()
    executor = executor_factory(agent)
    request = request_for(executor)
    response = await executor.handle(request)
    run = response.run
    assert run.status.value == "completed" and run.engine_version == "hierarchical"
    assert not executor.repo.replan_history(run.run_id)
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
    [history] = client.get(f"{base}/tasks?include_history=true").json()
    [attempt] = history["attempts"]
    assert attempt["generation"] == 1 and attempt["plan_id"] == plan["plan_id"]
    assert attempt["actions"][0]["decision_sequence"] == 1 and not attempt["input_bindings"]
    assert client.get(f"{base}/replans").json() == []
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
