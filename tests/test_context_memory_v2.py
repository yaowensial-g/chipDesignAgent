from pathlib import Path
import sqlite3

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


def test_long_conversation_is_incremental_lossless_and_budgeted(services) -> None:
    db, sessions, memories, documents, summaries, tokens = services
    _conversation(sessions, "long")
    for index in range(140):
        sessions.add_message(
            "long",
            "user" if index % 2 == 0 else "assistant",
            f"message-{index} " + ("constraint-data " * 8),
            agent_role="design",
        )
    current = sessions.add_message("long", "user", "current request", agent_role="design")
    recent = sessions.recent_messages(
        "long", agent_role="design", exclude_message_id=current.message_id, limit=24
    )
    summaries.refresh("long", "design", recent)
    first = sessions.get_summary("long", "design")
    assert first is not None
    assert first.covered_until_sequence == recent[0].sequence - 1
    assert len(first.source_message_ids) == len(set(first.source_message_ids))
    assert first.generation_mode == "deterministic_extract"
    assert first.summary_json["unprocessed_semantic_messages"][-1]["message_id"] == first.source_message_ids[-1]

    summaries.refresh("long", "design", recent)
    repeated = sessions.get_summary("long", "design")
    assert repeated.covered_until_sequence == first.covered_until_sequence
    assert repeated.source_message_ids == first.source_message_ids
    assert db.fetch_one(
        "SELECT COUNT(*) AS count FROM conversation_messages WHERE session_id = ?", ("long",)
    )["count"] == 141

    builder = ContextBuilderService(
        db, sessions, documents, memories, summaries, tokens,
        budget_config=ContextBudgetConfig(
            total=4_000,
            system_and_tools=400,
            current_request_and_run=300,
            recent_messages=900,
            summary=500,
            long_term_memory=300,
            documents=1_200,
            output_reserve=400,
        ),
    )
    result = builder.build(
        "long",
        ChatRequest(project_id="project", module_name="uart", message="current request"),
        "design",
        current.message_id,
    )
    assert result.estimated_tokens <= 4_000
    assert result.excluded


def test_role_summaries_are_isolated_but_shared_fact_is_visible(services) -> None:
    _, sessions, memories, _, summaries, _ = services
    _conversation(sessions, "roles")
    design_ids = []
    for index in range(35):
        design_ids.append(sessions.add_message(
            "roles", "user", f"DESIGN-SECRET-{index}", agent_role="design"
        ).message_id)
    design_recent = sessions.recent_messages("roles", agent_role="design", limit=24)
    summaries.refresh("roles", "design", design_recent)

    sessions.set_role("roles", UserRole.VERIFICATION_ENGINEER)
    verification_ids = []
    for index in range(35):
        verification_ids.append(sessions.add_message(
            "roles", "user", f"VERIFY-PRIVATE-{index}", agent_role="verification"
        ).message_id)
    verification_recent = sessions.recent_messages("roles", agent_role="verification", limit=24)
    summaries.refresh("roles", "verification", verification_recent)
    verification_summary = sessions.get_summary("roles", "verification")
    assert verification_summary is not None
    assert set(verification_summary.source_message_ids) <= set(verification_ids)
    assert "DESIGN-SECRET" not in verification_summary.summary_text

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


def test_constraint_extraction_promotion_and_cross_session_retrieval(services) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "source")
    message = sessions.add_message(
        "source", "user",
        "UART clock = 100 MHz，后续设计按照这个约束执行。",
        agent_role="design",
    )
    extractor = MemoryExtractorService(db, memories)
    first = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content,
    )
    second = extractor.extract_from_message(
        message_id=message.message_id, project_id="project", module_name="uart",
        agent_role="design", content=message.content,
    )
    assert len(first) == 1
    assert second[0].memory_id == first[0].memory_id
    assert first[0].status == "proposed"

    active = memories.promote(first[0].memory_id, MemoryPromoteRequest(
        authority="user_confirmed",
        source_refs=[{"source_type": "user_confirmation", "source_id": message.message_id}],
    ))
    assert active.status == "active"
    assert memories.retrieve("project", "uart", "UART clock frequency")[0].memory_id == active.memory_id


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


def test_summary_failure_does_not_advance_cursor(services, monkeypatch) -> None:
    db, sessions, memories, documents, summaries, tokens = services
    _conversation(sessions, "failure")
    for index in range(30):
        sessions.add_message("failure", "user", f"message-{index}", agent_role="design")
    recent = sessions.recent_messages("failure", agent_role="design", limit=24)

    monkeypatch.setattr(summaries, "_summarize", lambda *args: (_ for _ in ()).throw(RuntimeError("model down")))
    summaries.refresh("failure", "design", recent)
    summary = sessions.get_summary("failure", "design")
    assert summary.covered_until_sequence == 0
    assert "model down" in summary.last_error

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
    # 摘要失败时，窗口外尚未覆盖的原文仍作为强制候选进入本轮上下文。
    assert len(result.message_ids) == 30


def test_memory_extraction_failure_is_audited_and_retry_is_idempotent(services, monkeypatch) -> None:
    db, sessions, memories, _, _, _ = services
    _conversation(sessions, "extract-failure")
    message = sessions.add_message(
        "extract-failure", "user", "后续设计必须使用异步复位", agent_role="design"
    )
    extractor = MemoryExtractorService(db, memories)
    monkeypatch.setattr(
        extractor, "_model_candidates", lambda content: (_ for _ in ()).throw(RuntimeError("provider down"))
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

    monkeypatch.setattr(extractor, "_model_candidates", lambda content: [{
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
