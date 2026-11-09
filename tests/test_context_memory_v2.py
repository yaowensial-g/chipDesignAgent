from pathlib import Path
import sqlite3
import threading

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


def test_constraint_extraction_promotion_and_cross_session_retrieval(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "source")
    message = sessions.add_message(
        "source", "user",
        "UART clock = 100 MHz，后续设计按照这个约束执行。",
        agent_role="design",
    )
    extractor = MemoryExtractorService(db, memories)
    model_calls = []

    def extract_clock(content, **context):
        model_calls.append((content, context))
        return [{
            "memory_key": "uart.clock_frequency", "kind": "fact",
            "content": "UART 时钟为 100 MHz", "confidence": 9, "importance": 9,
        }]

    # 不再依赖时钟正则；stub 代表一次结构化模型判断。
    monkeypatch.setattr(extractor, "_model_candidates", extract_clock)
    first = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="source",
    )
    second = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="source",
    )
    assert len(first) == 1
    assert len(model_calls) == 1
    assert second[0].memory_id == first[0].memory_id
    assert first[0].status == "proposed"
    assert first[0].source_refs[0]["source_type"] == "conversation_message"
    assert first[0].source_refs[0]["source_id"] == message.message_id

    active = memories.promote(first[0].memory_id, MemoryPromoteRequest(
        authority="user_confirmed",
        source_refs=[{"source_type": "user_confirmation", "source_id": message.message_id}],
    ))
    assert active.status == "active"
    assert memories.retrieve("project", "uart", "UART clock frequency")[0].memory_id == active.memory_id


def test_message_model_handles_non_keyword_and_multiple_constraints(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "model-all")
    extractor = MemoryExtractorService(db, memories, sessions)
    calls = []

    def model_stub(content, **context):
        calls.append((content, context))
        if content.startswith("FIFO"):
            return [{
                "memory_key": "uart.fifo_depth", "kind": "fact",
                "content": "UART FIFO 深度为 32", "confidence": 9, "importance": 8,
            }]
        return [
            {"memory_key": "uart.clock_frequency", "kind": "fact", "content": "UART 时钟为 50 MHz"},
            {"memory_key": "uart.fifo_depth", "kind": "fact", "content": "UART FIFO 深度为 32"},
            {"memory_key": "uart.reset_polarity", "kind": "fact", "content": "UART 复位低有效"},
        ]

    monkeypatch.setattr(extractor, "_model_candidates", model_stub)
    fifo_message = sessions.add_message(
        "model-all", "user", "FIFO 深度设为 32，满的时候拉低 ready。", agent_role="design"
    )
    fifo = extractor.extract_from_message(
        message_id=fifo_message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=fifo_message.content, session_id="model-all",
    )
    assert [item.memory_key for item in fifo] == ["uart.fifo_depth"]

    combined_message = sessions.add_message(
        "model-all", "user", "UART 时钟为 50 MHz，FIFO 深度设为 32，复位低有效。",
        agent_role="design",
    )
    combined = extractor.extract_from_message(
        message_id=combined_message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=combined_message.content, session_id="model-all",
    )
    assert {item.memory_key for item in combined} == {
        "uart.clock_frequency", "uart.fifo_depth", "uart.reset_polarity",
    }
    assert all(item.status == "proposed" for item in combined)
    assert all(
        combined_message.message_id in {source["source_id"] for source in item.source_refs}
        for item in combined
    )
    assert len(calls) == 2


def test_question_reaches_model_but_empty_result_writes_no_memory(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "question")
    message = sessions.add_message(
        "question", "user", "UART 时钟为 50 MHz 可以吗？", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    calls = []
    monkeypatch.setattr(
        extractor,
        "_model_candidates",
        lambda content, **context: calls.append((content, context)) or [],
    )
    assert extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="question",
    ) == []
    assert len(calls) == 1
    event = db.fetch_one(
        "SELECT status, memory_ids_json FROM memory_extraction_events WHERE extraction_key = ?",
        (f"conversation_message:{message.message_id}",),
    )
    assert event["status"] == "completed"
    assert db.loads(event["memory_ids_json"], []) == []
    assert memories.list("project") == []


def test_non_message_event_reuses_model_candidate_path(services, monkeypatch) -> None:
    db, sessions, memories, documents, _, _ = services
    version = documents.ingest(DocumentIngestRequest(
        project_id="project", module_name="uart", kind=DocumentKind.AS,
        name="UART AS", version="1.0", content="UART 数据位宽为 8 bit",
    ))
    extractor = MemoryExtractorService(db, memories, sessions)
    captured = {}

    def extract_document(content, **context):
        captured.update(context)
        return [{
            "memory_key": "uart.data_width", "kind": "fact",
            "content": "UART 数据位宽为 8 bit", "confidence": 9, "importance": 8,
        }]

    monkeypatch.setattr(extractor, "_model_candidates", extract_document)
    created = extractor.extract_from_event(
        source_type="document_version", source_id=version.version_id,
        project_id="project", module_name="uart", agent_role=None,
        content="UART 数据位宽为 8 bit",
    )
    assert captured["source_type"] == "document_version"
    assert "recent_messages" not in captured
    assert created[0].source_refs[0]["source_id"] == version.version_id
    assert created[0].status == "proposed"


def test_message_context_is_role_filtered_and_supports_reference_resolution(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "reference")
    sessions.add_message(
        "reference", "assistant", "方案一深度 16，方案二深度 64。", agent_role="design"
    )
    sessions.add_message(
        "reference", "assistant", "VERIFY-PRIVATE: 使用深度 128。", agent_role="verification"
    )
    memories.create(MemoryCreateRequest(
        project_id="project", module_name="uart", memory_key="uart.fifo_depth",
        kind="fact", content="UART FIFO 当前深度为 16", status="active", importance=9,
        authority="formal", source_refs=[{"source_type": "external", "source_id": "SPEC-FIFO"}],
    ))
    current = sessions.add_message(
        "reference", "user", "那就用第二种，深度改成 32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    captured = {}

    def resolve_reference(content, **context):
        captured.update(context)
        return [{
            "memory_key": "uart.fifo_depth", "kind": "decision",
            "content": "UART FIFO 深度改为 32", "confidence": 9, "importance": 9,
        }]

    monkeypatch.setattr(extractor, "_model_candidates", resolve_reference)
    created = extractor.extract_from_message(
        message_id=current.message_id, project_id="project", module_name="uart",
        agent_role="design", content=current.content, session_id="reference",
    )
    visible_text = "\n".join(item["content"] for item in captured["recent_messages"])
    assert "方案一深度 16" in visible_text
    assert "VERIFY-PRIVATE" not in visible_text
    assert current.content not in visible_text
    assert [item["memory_key"] for item in captured["active_memories"]] == ["uart.fifo_depth"]
    assert created[0].memory_key == "uart.fifo_depth"
    assert created[0].status == "needs_review"


def test_temporary_assumption_approval_cannot_become_fact(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "assumption")
    message = sessions.add_message(
        "assumption", "user", "允许按临时假设继续", agent_role="design",
        message_type="run_decision", metadata={"action": "accept_assumption"},
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [{
        "memory_key": "uart.unverified_reset", "kind": "fact",
        "content": "UART 暂按异步复位处理", "confidence": 4, "importance": 5,
    }])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="assumption",
        temporary_assumption=True,
    )
    assert created[0].kind == "assumption"
    assert created[0].status == "proposed"


def test_candidate_batch_exact_duplicates_create_one_memory(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-exact")
    message = sessions.add_message(
        "dedup-exact", "user", "FIFO 深度为 32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [
        {"memory_key": "uart.fifo_depth", "kind": "fact", "content": "FIFO 深度为 32", "confidence": 6, "importance": 7},
        {"memory_key": "uart.fifo_depth", "kind": "fact", "content": "FIFO 深度为 32", "confidence": 9, "importance": 8},
    ])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-exact",
    )
    assert len(created) == 1
    assert created[0].confidence == 9
    assert created[0].importance == 8
    assert len(memories.list("project", "uart")) == 1


def test_candidate_batch_normalizes_memory_key_variants(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-key")
    message = sessions.add_message(
        "dedup-key", "user", "FIFO 深度为 32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [
        {"memory_key": "fifo.depth", "kind": "fact", "content": "FIFO 深度为32"},
        {"memory_key": "fifo_depth", "kind": "fact", "content": "FIFO 深度为32"},
        {"memory_key": "fifo-depth", "kind": "fact", "content": "FIFO 深度为32"},
    ])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-key",
    )
    assert len(created) == 1
    assert created[0].memory_key == "fifo.depth"


def test_candidate_batch_normalizes_safe_chinese_expression_variants(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-content")
    message = sessions.add_message(
        "dedup-content", "user", "FIFO 深度是32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [
        {"memory_key": "fifo.depth", "kind": "fact", "content": "FIFO 深度是32"},
        {"memory_key": "fifo_depth", "kind": "fact", "content": "FIFO 的深度设置为32"},
    ])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-content",
    )
    assert len(created) == 1


def test_candidate_batch_keeps_different_engineering_attributes(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-attributes")
    message = sessions.add_message(
        "dedup-attributes", "user", "UART 时钟为50MHz，FIFO 深度为32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [
        {"memory_key": "uart.clock_frequency", "kind": "fact", "content": "UART 时钟为50MHz"},
        {"memory_key": "uart.fifo_depth", "kind": "fact", "content": "UART FIFO 深度为32"},
    ])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-attributes",
    )
    assert {item.memory_key for item in created} == {
        "uart.clock_frequency", "uart.fifo_depth",
    }


def test_candidate_batch_conflicting_values_require_review(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-conflict")
    message = sessions.add_message(
        "dedup-conflict", "user", "FIFO 深度可能是 32 或 64。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [
        {"memory_key": "fifo.depth", "kind": "fact", "content": "FIFO 深度为32"},
        {"memory_key": "fifo_depth", "kind": "fact", "content": "FIFO 深度为64"},
    ])
    created = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-conflict",
    )
    assert len(created) == 2
    assert {item.status for item in created} == {"needs_review"}
    assert all("规范化 memory_key" in (item.review_reason or "") for item in created)


def test_cross_event_duplicate_merges_sources_without_overwriting_memory(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-events")
    message = sessions.add_message(
        "dedup-events", "user", "UART 时钟为 50 MHz。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    candidate = [{
        "memory_key": "uart.clock_frequency", "kind": "fact",
        "content": "UART 时钟为 50 MHz", "confidence": 8, "importance": 9,
    }]
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: candidate)
    from_message = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-events",
    )[0]
    db.execute(
        """INSERT INTO runs
        (run_id, session_id, project_id, module_name, run_type, status, user_instruction,
         route_reason, result_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, '{}', ?, ?)""",
        (
            "run-dedup", "dedup-events", "project", "uart", "orchestration",
            "确认 UART 时钟", "test", "2026-11-09T00:00:00Z", "2026-11-09T00:00:00Z",
        ),
    )
    from_run = extractor.extract_from_event(
        source_type="run", source_id="run-dedup", project_id="project",
        module_name="uart", agent_role="design", content="UART 时钟为 50 MHz。",
    )[0]
    assert from_run.memory_id == from_message.memory_id
    assert from_run.status == from_message.status == "proposed"
    assert from_run.authority == from_message.authority == "user_event"
    assert len(memories.list("project", "uart")) == 1
    assert {(source["source_type"], source["source_id"]) for source in from_run.source_refs} == {
        ("conversation_message", message.message_id), ("run", "run-dedup"),
    }


def test_failed_event_retry_is_order_independent(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "dedup-retry")
    message = sessions.add_message(
        "dedup-retry", "user", "UART 时钟为50MHz，FIFO 深度为32。", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories, sessions)
    clock = {"memory_key": "uart.clock_frequency", "kind": "fact", "content": "UART 时钟为50MHz"}
    fifo = {"memory_key": "uart.fifo_depth", "kind": "fact", "content": "UART FIFO 深度为32"}
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [clock, fifo])
    original_create = memories.create_or_merge_candidate
    calls = 0

    def fail_second_candidate(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected partial failure")
        return original_create(request)

    monkeypatch.setattr(memories, "create_or_merge_candidate", fail_second_candidate)
    assert extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-retry",
    ) == []
    first_clock = memories.list("project", "uart")[0]
    assert first_clock.memory_key == "uart.clock_frequency"

    monkeypatch.setattr(memories, "create_or_merge_candidate", original_create)
    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [fifo, clock])
    retried = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content, session_id="dedup-retry",
    )
    assert len(retried) == 2
    assert len(memories.list("project", "uart")) == 2
    assert next(item for item in retried if item.memory_key == "uart.clock_frequency").memory_id == first_clock.memory_id
    assert all(item.extraction_key for item in retried)
    event = db.fetch_one(
        "SELECT status, attempts FROM memory_extraction_events WHERE extraction_key = ?",
        (f"conversation_message:{message.message_id}",),
    )
    assert event["status"] == "completed"
    assert event["attempts"] == 2


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
    scoped = service._scoped_context(
        {"project_id": "project", "module_name": "uart", "history": ["secret"]}, contract
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


def test_memory_extraction_failure_is_audited_and_retry_is_idempotent(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "extract-failure")
    message = sessions.add_message(
        "extract-failure", "user", "后续设计必须使用异步复位", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories)
    monkeypatch.setattr(
        extractor, "_model_candidates",
        lambda content, **context: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    assert extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content,
    ) == []
    event = db.fetch_one(
        "SELECT * FROM memory_extraction_events WHERE extraction_key = ?",
        (f"conversation_message:{message.message_id}",),
    )
    assert event["status"] == "failed"
    assert "provider down" in event["error"]

    monkeypatch.setattr(extractor, "_model_candidates", lambda content, **context: [{
        "memory_key": "uart.reset_policy", "kind": "decision",
        "content": "UART 使用异步复位", "confidence": 8, "importance": 8,
    }])
    retried = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content,
    )
    repeated = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content,
    )
    assert len(retried) == 1
    assert repeated[0].memory_id == retried[0].memory_id


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
