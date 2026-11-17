import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
from time import monotonic, sleep
from uuid import uuid4

import pytest

from app.domain.enums import AgentRole, DocumentKind, UserRole
from app.domain.models import (
    ChatRequest,
    DelegationRequest,
    DocumentIngestRequest,
    MemoryCreateRequest,
    MemoryPromoteRequest,
)
from app.services.context_builder_service import ContextBudgetConfig, ContextBuilderService
from app.services.conversation_summary_service import ConversationSummaryService
from app.services.delegation_service import DelegationService
from app.services.document_service import DocumentService
from app.services.memory_extractor_service import MemoryExtractorService
from app.services.memory_service import MemoryService
from app.services.run_service import RunService
from app.services.session_service import SessionService
from app.services.token_service import TokenCounter
from app.storage.database import Database


@pytest.fixture
def services(tmp_path: Path):
    db = Database(tmp_path / "memory-v2.db")
    sessions = SessionService(db)
    memories = MemoryService(db)
    tokens = TokenCounter()
    documents = DocumentService(db, tokens, memories)
    summaries = ConversationSummaryService(sessions, tokens)
    return db, sessions, memories, documents, summaries, tokens


def _conversation(sessions: SessionService, session_id: str, role=UserRole.DESIGN_ENGINEER):
    return sessions.resolve(session_id, "project", role, "uart")


def _turn(sessions: SessionService, session_id: str, index: int, role: str = "design"):
    user = sessions.add_message(
        session_id, "user", f"user-{index}", agent_role=role
    )
    assistant = sessions.add_message(
        session_id, "assistant", f"assistant-{index}", agent_role=role,
        turn_id=user.turn_id,
    )
    return user, assistant


def test_context_uses_five_raw_turns_and_thirty_independent_summaries(services) -> None:
    db, sessions, memories, documents, summaries, tokens = services
    _conversation(sessions, "long")
    turns = [_turn(sessions, "long", index) for index in range(40)]
    completed = sessions.completed_turns("long", agent_role="design")
    assert len(completed) == 40
    for turn in completed:
        summary = summaries.summarize_turn(turn.turn_id, "design")
        assert summary.status == "completed"
        assert summary.user_message_id == turn.user.message_id
        assert summary.assistant_message_id == turn.assistant.message_id
    current = sessions.add_message("long", "user", "current request", agent_role="design")
    assert db.fetch_one(
        "SELECT COUNT(*) AS count FROM conversation_messages WHERE session_id = ?", ("long",)
    )["count"] == 81

    builder = ContextBuilderService(
        db, sessions, documents, memories, summaries, tokens,
    )
    result = builder.build(
        "long",
        ChatRequest(project_id="project", module_name="uart", message="current request"),
        "design",
        current.message_id,
    )
    assert result.raw_turn_ids == [turn[0].turn_id for turn in turns[-5:]]
    assert len(result.message_ids) == 10
    assert len(result.turn_summary_ids) == 30
    assert result.summary["turn_ids"] == [
        turn[0].turn_id for turn in turns[5:35]
    ]
    assert current.message_id not in result.message_ids
    assert result.estimated_tokens <= 32_000


def test_role_summaries_are_isolated_but_shared_fact_is_visible(services) -> None:
    _, sessions, memories, _, summaries, _ = services
    _conversation(sessions, "roles")
    design_turns = [_turn(sessions, "roles", index, "design") for index in range(6)]
    for turn in sessions.completed_turns("roles", agent_role="design"):
        summaries.summarize_turn(turn.turn_id, "design")

    sessions.set_role("roles", UserRole.VERIFICATION_ENGINEER)
    verification_turns = [
        _turn(sessions, "roles", index, "verification") for index in range(6)
    ]
    for turn in sessions.completed_turns("roles", agent_role="verification"):
        summaries.summarize_turn(turn.turn_id, "verification")
    verification_summaries = summaries.get_recent_turn_summaries(
        "roles", "verification", limit=30
    )
    assert {item.turn_id for item in verification_summaries} == {
        turn[0].turn_id for turn in verification_turns
    }
    assert not ({turn[0].turn_id for turn in design_turns} & {
        item.turn_id for item in verification_summaries
    })

    memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.clock_frequency",
        kind="fact", content="UART clock 为 100 MHz", confidence=10, importance=9,
        authority="formal", status="active",
        source_refs=[{"source_type": "external", "source_id": "SPEC-1"}],
    ))
    shared = memories.retrieve(
        "project", "uart", "UART clock", agent_role="verification"
    )
    assert [item.memory_key for item in shared] == ["uart.clock_frequency"]


def test_cross_event_dedup_respects_role_private_owner(services) -> None:
    _, _, memories, _, _, _ = services
    base = dict(
        project_id="project", module_name="uart", scope="module",
        memory_key="uart.private_plan", kind="decision", content="UART 采用两级流水",
        visibility="role_private", status="proposed", authority="agent",
    )
    design = memories.create_or_merge_candidate(MemoryCreateRequest(
        **base, owner_agent_role="design", extraction_key="private-design",
        source_refs=[{"source_type": "external", "source_id": "DESIGN-NOTE"}],
    ))
    verification = memories.create_or_merge_candidate(MemoryCreateRequest(
        **base, owner_agent_role="verification", extraction_key="private-verification",
        source_refs=[{"source_type": "external", "source_id": "VERIFICATION-NOTE"}],
    ))
    assert design.memory_id != verification.memory_id
    assert len(memories.list("project", "uart")) == 2


def test_conflicting_confirmed_constraint_supersedes_old_fact(services) -> None:
    _, sessions, memories, _, _, _ = services
    _conversation(sessions, "conflict")
    old = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.clock_frequency",
        kind="fact", content="UART clock 为 100 MHz", confidence=10, importance=9,
        authority="formal", status="active",
        source_refs=[{"source_type": "external", "source_id": "SPEC-OLD"}],
    ))
    message = sessions.add_message(
        "conflict", "user", "UART clock 确定为 125 MHz", agent_role="design"
    )
    proposed = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.clock_frequency",
        kind="fact", content="UART clock 为 125 MHz", confidence=10, importance=9,
        authority="user_event", status="proposed",
        source_refs=[{"source_type": "conversation_message", "source_id": message.message_id}],
    ))
    assert proposed.status == "needs_review"
    new = memories.promote(proposed.memory_id, MemoryPromoteRequest(
        authority="user_confirmed",
        source_refs=[{"source_type": "user_confirmation", "source_id": message.message_id}],
    ))
    assert memories.get(old.memory_id).status == "superseded"
    assert new.status == "active"
    retrieved = memories.retrieve("project", "uart", "UART clock")
    assert [item.memory_id for item in retrieved] == [new.memory_id]


def test_document_upgrade_invalidates_only_affected_source_and_is_atomic(services) -> None:
    db, _, memories, documents, _, _ = services
    v1 = documents.ingest(DocumentIngestRequest(
        project_id="project", module_name="uart", kind=DocumentKind.AS,
        name="UART AS", version="1.0", content="UART clock 为 100 MHz",
    ))
    memory = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.clock_frequency",
        kind="fact", content="UART clock 为 100 MHz", status="active",
        authority="AS", importance=9,
        source_refs=[{"source_type": "document_version", "source_id": v1.version_id}],
    ))
    independently_supported = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.reset_policy",
        kind="fact", content="UART 使用同步复位", status="active", authority="approved",
        source_refs=[
            {"source_type": "document_version", "source_id": v1.version_id},
            {"source_type": "external", "source_id": "ISSUE-APPROVED"},
        ],
    ))
    v2 = documents.ingest(DocumentIngestRequest(
        project_id="project", module_name="uart", kind=DocumentKind.AS,
        name="UART AS", version="2.0", content="UART clock 为 125 MHz",
    ))
    assert v2.status == "active"
    stale = memories.get(memory.memory_id)
    assert stale.status == "stale"
    assert stale.source_refs[0]["status"] == "invalidated"
    supported = memories.get(independently_supported.memory_id)
    assert supported.status == "active"
    assert "仍有独立有效来源" in supported.review_reason

    class FailingInvalidation:
        def invalidate_sources(self, *args, **kwargs):
            raise RuntimeError("injected failure")

    documents.memories = FailingInvalidation()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="injected failure"):
        documents.ingest(DocumentIngestRequest(
            project_id="project", module_name="uart", kind=DocumentKind.AS,
            name="UART AS", version="3.0", content="UART clock 为 150 MHz",
        ))
    assert documents.get_version(v2.version_id).status == "active"
    assert not db.fetch_one(
        "SELECT version_id FROM document_versions WHERE document_id = ? AND version = '3.0'",
        (v2.document_id,),
    )


def test_delegate_receives_shared_memory_but_not_private_history(services) -> None:
    db, _, memories, _, _, _ = services
    memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.clock_frequency",
        kind="fact", content="UART clock 为 100 MHz", status="active", importance=9,
        authority="formal", source_refs=[{"source_type": "external", "source_id": "SPEC"}],
    ))
    memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.private_plan",
        kind="inference", content="Design 私有实现草案", status="active", importance=9,
        visibility="role_private", owner_agent_role="design", authority="agent",
    ))
    service = DelegationService(db=db, memories=memories)
    contract = DelegationRequest(
        parent_run_id="run", from_agent=AgentRole.DESIGN,
        to_agent=AgentRole.VERIFICATION, task_type="review", instruction="检查 UART clock",
    )
    from app.domain.context import ContextBuildRequest, ContextPurpose

    scoped = service.contexts.specialist_scope(
        ContextBuildRequest(ContextPurpose.SPECIALIST_ANALYSIS, project_id="project", module_name="uart",
                            agent_role="verification", task_input={"request": {"history": ["secret"]}}),
        contract,
    )
    assert scoped["history"] == []
    assert [item["memory_key"] for item in scoped["_memory_context"]] == ["uart.clock_frequency"]


def test_failed_or_missing_turn_summaries_fall_back_to_complete_raw_turns(
    services, monkeypatch
) -> None:
    db, sessions, memories, documents, summaries, tokens = services
    _conversation(sessions, "failure")
    turns = [_turn(sessions, "failure", index) for index in range(10)]
    first = sessions.completed_turns("failure", agent_role="design")[0]
    monkeypatch.setattr(
        summaries, "_generate",
        lambda turn: (_ for _ in ()).throw(RuntimeError("model down")),
    )
    failed = summaries.summarize_turn(first.turn_id, "design")
    assert failed.status == "failed"
    assert "model down" in (failed.last_error or "")

    current = sessions.add_message(
        "failure", "user", "current", agent_role="design"
    )
    builder = ContextBuilderService(db, sessions, documents, memories, summaries, tokens)
    result = builder.build(
        "failure",
        ChatRequest(project_id="project", module_name="uart", message="current"),
        "design",
        current.message_id,
    )
    # 较早五轮包含 failed 和尚未生成两种状态，全部回退为完整 User+Assistant。
    assert result.turn_summary_ids == []
    assert result.raw_turn_ids == [turn[0].turn_id for turn in turns]
    assert len(result.message_ids) == 20
    assert len(result.message_ids) % 2 == 0
    retried = []
    monkeypatch.setattr(
        summaries, "enqueue_turn",
        lambda turn_id, session_id, agent_role: retried.append(turn_id) or True,
    )
    assert summaries.retry_failed_summaries(session_id="failure") == 1
    assert retried == [first.turn_id]


def test_turn_summary_is_idempotent_and_background_queue_uses_worker(services, monkeypatch) -> None:
    db, sessions, _, _, summaries, _ = services
    _conversation(sessions, "summary-idempotent")
    user, _ = _turn(sessions, "summary-idempotent", 1)
    first = summaries.summarize_turn(user.turn_id, "design")
    second = summaries.summarize_turn(user.turn_id, "design")
    assert first.id == second.id
    assert second.attempts == 1
    assert db.fetch_one(
        "SELECT COUNT(*) AS count FROM conversation_turn_summaries WHERE turn_id = ?",
        (user.turn_id,),
    )["count"] == 1
    turn = sessions.get_turn(user.turn_id, agent_role="design")
    validated = summaries._validate({
        "decisions": [{
            "content": "Assistant 建议采用两级流水",
            "source_message_ids": [turn.assistant.message_id],
            "confirmation_status": "user_confirmed",
        }],
    }, turn)
    assert validated["decisions"][0]["confirmation_status"] == "unconfirmed"

    other_user, _ = _turn(sessions, "summary-idempotent", 2)
    worker_finished = threading.Event()
    worker_names = []

    def worker(turn_id, agent_role=None):
        worker_names.append(threading.current_thread().name)
        worker_finished.set()
        return summaries.get_turn_summary(turn_id)

    monkeypatch.setattr(summaries, "summarize_turn", worker)
    assert summaries.enqueue_turn(other_user.turn_id, "summary-idempotent", "design")
    assert worker_finished.wait(2)
    assert worker_names[0].startswith("chipflow-summary")


def test_turn_summary_hard_limit_uses_compression_before_save(services, monkeypatch) -> None:
    _, sessions, _, _, summaries, _ = services
    _conversation(sessions, "summary-limit")
    user, _ = _turn(sessions, "summary-limit", 1)
    oversized = {
        "topic": "oversized summary token " * 2_000, "module": "", "user_intent": "",
        "key_information": [], "decisions": [], "constraints": [],
        "assumptions": [], "inferences": [], "open_questions": [], "keywords": [],
    }
    compact = {
        "topic": "短摘要", "module": "uart", "user_intent": "检查接口",
        "key_information": [], "decisions": [], "constraints": [],
        "assumptions": [], "inferences": [], "open_questions": [], "keywords": [],
    }
    monkeypatch.setattr(summaries, "_generate", lambda turn: (oversized, "stub"))
    monkeypatch.setattr(summaries, "_compress", lambda turn, summary: compact)
    result = summaries.summarize_turn(user.turn_id, "design")
    assert result.status == "completed"
    assert result.token_count <= 500
    assert result.summary_json["topic"] == "短摘要"


def test_turn_budget_never_splits_user_and_assistant_and_manifest_traces_turns(services) -> None:
    db, sessions, memories, documents, summaries, tokens = services
    _conversation(sessions, "turn-budget")
    turns = [_turn(sessions, "turn-budget", index) for index in range(7)]
    for turn in sessions.completed_turns("turn-budget", agent_role="design")[:2]:
        summaries.summarize_turn(turn.turn_id, "design")
    current = sessions.add_message("turn-budget", "user", "current", agent_role="design")
    builder = ContextBuilderService(
        db, sessions, documents, memories, summaries, tokens,
        budget_config=ContextBudgetConfig(
            total=2_000, system_and_tools=200, current_request_and_run=200,
            recent_messages=40, summary=80, long_term_memory=100,
            documents=500, output_reserve=200,
        ),
    )
    result = builder.build(
        "turn-budget",
        ChatRequest(project_id="project", module_name="uart", message="current"),
        "design",
        current.message_id,
    )
    assert len(result.message_ids) % 2 == 0
    selected = set(result.message_ids)
    for user, assistant in turns:
        assert (user.message_id in selected) == (assistant.message_id in selected)
    manifest = builder.save_manifest("turn-budget", "audit-run", "design", result)
    assert manifest.turn_summary_ids == result.turn_summary_ids
    assert manifest.raw_turn_ids == result.raw_turn_ids
    assert manifest.sections["raw_conversation"]["tokens"] >= 0


@pytest.fixture
def extractor_factory(services):
    db, sessions, memories, _, _, tokens = services
    workers = []

    def make(**kwargs):
        worker = MemoryExtractorService(db, memories, sessions, tokens, **kwargs)
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker._executor.shutdown(wait=True)


def _new_run(services, request="UART FIFO throughput", *, project="project", module="uart", role=AgentRole.DESIGN):
    db, sessions, *_ = services
    run_service = RunService(db)
    session_id = f"memory-v2-{uuid4()}"
    user_role = UserRole.DESIGN_ENGINEER if role == AgentRole.DESIGN else UserRole.VERIFICATION_ENGINEER
    sessions.resolve(session_id, project, user_role, module)
    run_id = run_service.create_orchestration_run(
        session_id, ChatRequest(project_id=project, module_name=module, message=request), {}, role,
    )
    message = sessions.add_message(session_id, "user", request, agent_role=role.value, run_id=run_id)
    return run_service, run_id, message


def _complete(services, run_info, response="Completed the UART FIFO throughput analysis."):
    runs, run_id, user = run_info
    run = runs.complete_reply(run_id, response)
    services[1].add_message(run.session_id, "assistant", response, agent_role=run.primary_agent.value,
                            run_id=run_id, turn_id=user.turn_id)
    return run


def _enqueue(worker, run):
    return worker.enqueue_run(run_id=run.run_id, project_id=run.project_id,
                              module_name=run.module_name, agent_role=run.primary_agent.value)


def _event(db, run_id):
    return db.fetch_one("SELECT * FROM memory_extraction_events WHERE extraction_key = ?", (f"run:{run_id}",))


def _wait_event(db, run_id, status):
    deadline = monotonic() + 8
    while monotonic() < deadline:
        row = _event(db, run_id)
        if row and row["status"] == status:
            return row
        sleep(0.02)
    pytest.fail(f"Expected {status} but got {_event(db, run_id)}")


def test_only_completed_run_with_persisted_assistant_can_enqueue(services, extractor_factory):
    db, *_ = services
    worker = extractor_factory()
    info = _new_run(services)
    runs, run_id, _ = info
    assert not _enqueue(worker, runs.get(run_id))
    assert not _enqueue(worker, runs.complete_reply(run_id, "not persisted in messages"))
    assert _event(db, run_id) is None
    run = _complete(services, info)
    assert not worker.enqueue_run(run_id=run_id, project_id="other", module_name="uart", agent_role="design")
    assert _enqueue(worker, run)
    assert _wait_event(db, run_id, "completed")["memory_ids_json"] == "[]"


def test_missing_production_extraction_model_keeps_event_retryable(services, extractor_factory, monkeypatch):
    db, *_ = services
    monkeypatch.setenv("CHIP_AGENT_RUNTIME_MODE", "production")
    run = _complete(services, _new_run(services))
    worker = extractor_factory()
    assert _enqueue(worker, run)
    event = _wait_event(db, run.run_id, "failed")
    assert "no configured model" in event["error"]
    assert event["attempts"] == 1


def test_enqueue_is_nonblocking_and_idempotent_across_workers(services, extractor_factory):
    db, *_ = services
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(prompt, payload):
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(8)
        return {"memories": []}

    run = _complete(services, _new_run(services))
    worker = extractor_factory(generate_memories=generate)
    other_worker = extractor_factory(generate_memories=generate)
    try:
        targets = [worker, other_worker] * 6
        with ThreadPoolExecutor(max_workers=6) as executor:
            requests = [executor.submit(_enqueue, target, run) for target in targets]
            assert all(request.result(timeout=4) for request in requests)
        assert entered.wait(4)
        assert _event(db, run.run_id)["status"] == "processing"
        assert len(calls) == 1
    finally:
        release.set()
    assert _wait_event(db, run.run_id, "completed")["attempts"] == 1
    assert _enqueue(worker, run)
    assert _enqueue(other_worker, run)
    assert len(calls) == 1
    assert calls[0].startswith("chipflow-memory")


def test_model_and_storage_failures_are_retryable_with_atomic_writes(services, extractor_factory, monkeypatch):
    db, _, memories, *_ = services
    calls = 0

    def generate(prompt, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected model failure")
        return {"memories": [
            {"action": "add", "content": "FIFO throughput reviews should include burst measurements."},
            {"action": "add", "content": "FIFO throughput reviews should examine backpressure."},
        ]}

    real_save = memories.save_semantic_memory

    def fail_second_save(*args, **kwargs):
        if "backpressure" in args[2]:
            raise RuntimeError("injected database failure")
        return real_save(*args, **kwargs)

    worker = extractor_factory(generate_memories=generate)
    run = _complete(services, _new_run(services))
    assert _enqueue(worker, run)
    first = _wait_event(db, run.run_id, "failed")
    assert first["attempts"] == 1 and "model failure" in first["error"]
    assert RunService(db).get(run.run_id).status.value == "completed"
    worker._executor.shutdown(wait=True)

    with monkeypatch.context() as patch:
        patch.setattr(memories, "save_semantic_memory", fail_second_save)
        second_worker = extractor_factory(generate_memories=generate)
        assert _enqueue(second_worker, run)
        second_worker._executor.shutdown(wait=True)
    second = _wait_event(db, run.run_id, "failed")
    assert second["attempts"] == 2 and "database failure" in second["error"]
    assert memories.list(run.project_id) == []

    restarted = extractor_factory(generate_memories=generate)
    assert _enqueue(restarted, run)
    final = _wait_event(db, run.run_id, "completed")
    assert final["attempts"] == 3 and final["error"] is None
    assert len(memories.list(run.project_id)) == 2


@pytest.mark.asyncio
async def test_queue_overflow_and_stale_processing_recover_on_startup(services, extractor_factory, monkeypatch):
    db, *_ = services
    entered, release = threading.Event(), threading.Event()
    first, overflow, interrupted = [_complete(services, _new_run(services)) for _ in range(3)]

    def slow_model(prompt, payload):
        entered.set()
        assert release.wait(8)
        return {"memories": []}

    worker = extractor_factory(max_workers=1, max_pending_tasks=1, generate_memories=slow_model)
    try:
        assert _enqueue(worker, first)
        assert entered.wait(4)
        assert not _enqueue(worker, overflow)
        assert not _enqueue(worker, interrupted)
        assert _event(db, overflow.run_id)["status"] == "pending"
        db.execute(
            """UPDATE memory_extraction_events SET status='processing', attempts=1, updated_at=?
            WHERE extraction_key=?""",
            ("2000-01-01T00:00:00+00:00", f"run:{interrupted.run_id}"),
        )
    finally:
        release.set()
        worker._executor.shutdown(wait=True)

    monkeypatch.setattr("app.services.memory_extractor_service.POLL_INTERVAL_SECONDS", 0.01)
    restarted = extractor_factory(max_workers=1, max_pending_tasks=1)
    recovery = asyncio.create_task(restarted.run_background())
    try:
        assert (await asyncio.to_thread(_wait_event, db, overflow.run_id, "completed"))["attempts"] == 1
        assert (await asyncio.to_thread(_wait_event, db, interrupted.run_id, "completed"))["attempts"] == 2
    finally:
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery


def test_run_context_and_extraction_contract(services, extractor_factory):
    db, sessions, memories, *_ = services
    initial_request = "UART FIFO: always review peak throughput in later designs."
    runs, run_id, root = _new_run(services, initial_request)
    sessions.add_message(root.session_id, "assistant", "Which reset strategy?", agent_role="design",
                         run_id=run_id, turn_id=root.turn_id)
    decision = sessions.add_message(
        root.session_id, "user", "Try a speculative dual-clock implementation for this run only.",
        agent_role="design", message_type="run_decision", run_id=run_id,
        metadata={"action": "accept_assumption"},
    )
    run = _complete(services, (runs, run_id, decision), "Measured improvement with skid-buffer burst tests.")
    for role, linked_run in (("design", "other-run"), ("verification", run_id)):
        sessions.add_message(run.session_id, "assistant", "UNRELATED PRIVATE TEXT", run_id=linked_run,
                             agent_role=role, turn_id=str(uuid4()))

    db.execute(
        """INSERT INTO workspaces (workspace_id, project_id, display_name, root_path, created_at)
        VALUES ('workspace', 'project', 'workspace', 'workspace', ?)""",
        (run.created_at,),
    )
    db.execute(
        """INSERT INTO tool_runs (run_id, workspace_id, tool_name, status, arguments_json, command_json,
        stdout, stderr, exit_code, created_at, updated_at)
        VALUES ('real-test', 'workspace', 'simulate', 'completed', '{}', '[]',
                'RAW OUTPUT', 'RAW ERROR', 0, ?, ?)""",
        (run.created_at, run.updated_at),
    )
    db.execute("UPDATE runs SET tool_run_id=?, result_json=? WHERE run_id=?", (
        "real-test", db.dumps({"tool_calls": [{"raw": "RAW TOOL CALL"}],
                              "observations": [{"thought": "HIDDEN REASONING"}]}), run_id,
    ))
    db.execute("INSERT INTO eda_tool_results VALUES (?, ?)",
               ("real-test", db.dumps({"status": "PASS", "metrics": {"burst_checks_passed": 3}})))

    config_calls = []

    def config(project, module, query):
        config_calls.append((project, module, query))
        return {"fifo_depth": 32, "irrelevant_blob": "large field " * 2_000, "revision": 7}

    approved = [
        "User prefers peak throughput measurements in future UART FIFO reviews.",
        "UART FIFO burst simulation with a skid buffer validated an improvement in throughput.",
        "Burst stress testing was chosen to reveal peak-load bottlenecks.",
    ]

    def generate(prompt, payload):
        assert payload["user_request"] == initial_request
        assert payload["assistant_response"] == "Measured improvement with skid-buffer burst tests."
        assert payload["project_config"] == {"revision": 7, "fifo_depth": 32}
        assert payload["run_summary"]["decisions"][-1]["action"] == "accept_assumption"
        [tool] = payload["run_summary"]["tool_results"]
        assert tool["verification_status"] == "PASS" and tool["metrics"] == {"burst_checks_passed": 3}
        rendered = db.dumps(payload)
        assert all(word not in rendered for word in ("RAW ", "HIDDEN REASONING", "UNRELATED PRIVATE TEXT"))
        for rule in ("稳定用户偏好", "设计理由", "实际工具结果", "权威工程数据", "原始日志", "未确认设想", "accept_assumption"):
            assert rule in prompt
        return {"memories": [{"action": "add", "content": content} for content in approved]}

    worker = extractor_factory(generate_memories=generate, read_project_config=config)
    assert _enqueue(worker, run)
    _wait_event(db, run_id, "completed")
    assert config_calls == [("project", "uart", initial_request + "\n" + decision.content)]
    assert {item.content for item in memories.list(run.project_id)} == set(approved)


def test_semantic_dedup_update_and_scope_checks(services, extractor_factory):
    db, _, memories, *_ = services
    content = "UART FIFO throughput reviews prioritize burst measurements."
    first = _complete(services, _new_run(services))
    first_id = memories.save_semantic_memory("project", "uart", content, first.run_id)
    assert memories.get(first_id).kind == "semantic"
    assert memories.get(first_id).status == "active"
    assert memories.get(first_id).memory_key is None
    second = _complete(services, _new_run(services))
    assert memories.save_semantic_memory("project", "uart", "  " + content.replace(" ", "  "), second.run_id) == first_id
    assert {ref["source_id"] for ref in memories.get(first_id).source_refs} == {first.run_id, second.run_id}

    outside = [
        _complete(services, _new_run(services, project="other")),
        _complete(services, _new_run(services, module="spi")),
        _complete(services, _new_run(services, role=AgentRole.VERIFICATION)),
    ]
    outside_ids = [
        memories.save_semantic_memory(run.project_id, run.module_name, content, run.run_id)
        for run in outside
    ]
    assert len(set([first_id, *outside_ids])) == 4
    with pytest.raises(ValueError, match="same scope"):
        memories.save_semantic_memory("project", "uart", content, outside[0].run_id)

    shared = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", kind="semantic", status="active",
        content="UART FIFO throughput baseline", source_refs=[{"source_type": "external", "source_id": "SPEC"}],
    ))
    updated = content + " Vary backpressure on every run."

    def generate(prompt, payload):
        assert {item["memory_id"] for item in payload["related_memories"]} == {first_id, shared.memory_id}
        return {"memories": [
            {"action": "update", "target_memory_id": invalid, "content": "Invalid change"}
            for invalid in ["invented", *outside_ids, shared.memory_id]
        ] + [{"action": "update", "target_memory_id": first_id, "content": updated}]}

    third = _complete(services, _new_run(services))
    worker = extractor_factory(generate_memories=generate)
    assert _enqueue(worker, third)
    assert db.loads(_wait_event(db, third.run_id, "completed")["memory_ids_json"], []) == [first_id]
    result = memories.get(first_id)
    assert result.content == updated
    assert {ref["source_id"]: ref["status"] for ref in result.source_refs} == {
        first.run_id: "invalidated", second.run_id: "invalidated", third.run_id: "active",
    }
    assert memories.get(shared.memory_id).content == shared.content
    assert all(memories.get(memory_id).content == content for memory_id in outside_ids)


def test_context_selects_five_active_semantic_memories_and_keeps_budget(services):
    db, sessions, memories, documents, summaries, tokens = services
    for index in range(7):
        run = _complete(services, _new_run(services))
        memories.save_semantic_memory("project", "uart", f"UART throughput preference {index}", run.run_id)
    memories.invalidate_sources([run.run_id])
    proposed = memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", kind="preference",
        content="UART throughput proposed", status="proposed",
    ))
    _conversation(sessions, "memory-context")
    request = ChatRequest(project_id="project", module_name="uart", message="UART throughput")
    current = sessions.add_message("memory-context", "user", request.message, agent_role="design")
    builder = ContextBuilderService(db, sessions, documents, memories, summaries, tokens)
    context = builder.build("memory-context", request, "design", current.message_id)
    assert len(context.memory_ids) == 5
    assert proposed.memory_id not in context.memory_ids
    assert all(set(item) == {"memory_id", "kind", "content"} for item in context.memory_context)
    assert all(item["kind"] == "semantic" and not item["content"].endswith("6") for item in context.memory_context)
    assert not builder.build("memory-context", request, "verification", current.message_id).memory_ids

    builder.budget = ContextBudgetConfig(long_term_memory=3)
    limited = builder.build("memory-context", request, "design", current.message_id)
    assert limited.memory_ids == []
    assert limited.estimated_tokens <= builder.token_budget


@pytest.mark.asyncio
async def test_document_and_delegation_have_no_independent_memory_event(services):
    db, _, memories, documents, *_ = services
    documents.ingest(DocumentIngestRequest(
        project_id="project", module_name="uart", kind=DocumentKind.AS,
        name="UART AS", version="1.0", content="UART specification",
    ))
    _, run_id, _ = _new_run(services)
    delegation = await DelegationService(db, memories).delegate(
        parent_run_id=run_id, from_agent=AgentRole.DESIGN, to_agent=AgentRole.ARCHITECTURE,
        task_type="review", instruction="Review UART FIFO", analyze=lambda _: {"summary": "Assess throughput."},
        request_payload={"project_id": "project", "module_name": "uart"},
    )
    assert delegation.status.value == "completed"
    assert not db.fetch_all("SELECT * FROM memory_extraction_events")
    assert not memories.list("project")


def test_schema_migration_is_repeatable_and_legacy_summary_is_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                user_role TEXT NOT NULL, primary_agent TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, module_name TEXT,
                scope TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                confidence INTEGER NOT NULL, authority TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE memory_sources (
                source_ref_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL,
                source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT,
                location TEXT, content_hash TEXT, source_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE context_manifests (
                context_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT,
                agent_role TEXT NOT NULL, token_budget INTEGER NOT NULL,
                estimated_tokens INTEGER NOT NULL, message_ids_json TEXT NOT NULL,
                memory_ids_json TEXT NOT NULL, chunk_ids_json TEXT NOT NULL,
                observation_ids_json TEXT NOT NULL, sections_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
    db = Database(path)
    sessions = SessionService(db)
    _conversation(sessions, "legacy")
    db.execute(
        """INSERT INTO conversation_summaries
        (session_id, summary, covered_until_sequence, updated_at)
        VALUES (?, ?, ?, ?)""",
        ("legacy", "mixed design and verification history", 10, "2026-01-01T00:00:00Z"),
    )
    # Re-running initialization is idempotent and keeps old data for audit.
    Database(path)
    assert db.fetch_one(
        "SELECT summary FROM conversation_summaries WHERE session_id = ?", ("legacy",)
    )["summary"] == "mixed design and verification history"
    assert sessions.get_summary("legacy", "design") is None
    columns = {
        row["name"] for row in db.fetch_all("PRAGMA table_info(memories)")
    }
    assert {"memory_key", "importance", "visibility", "review_reason"} <= columns
    assert "turn_id" in {
        row["name"] for row in db.fetch_all("PRAGMA table_info(conversation_messages)")
    }
    assert {"turn_summary_ids_json", "raw_turn_ids_json"} <= {
        row["name"] for row in db.fetch_all("PRAGMA table_info(context_manifests)")
    }
    assert db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("conversation_turn_summaries",),
    ) is not None
