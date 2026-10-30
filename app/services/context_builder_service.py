"""按 Token 预算组装模型上下文，并记录可审计的 Context Manifest。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.domain.models import (
    ChatHistoryMessage,
    ChatRequest,
    ContextManifestView,
    DocumentIngestRequest,
    DocumentInput,
    RetrievalRequest,
)
from app.services.document_service import DocumentService, document_service
from app.services.memory_service import MemoryService, memory_service
from app.services.session_service import SessionService, session_service
from app.services.token_service import TokenCounter, token_counter
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


@dataclass
class ContextBuildResult:
    """一次上下文选择结果；effective_request 才会被发送给模型。"""

    effective_request: ChatRequest
    message_ids: list[str]
    memory_ids: list[str]
    chunk_ids: list[str]
    memory_context: list[dict]
    estimated_tokens: int
    sections: dict[str, object]


class ContextBuilderService:
    """把短期历史、摘要、长期记忆和 RAG 片段组合成有界上下文。"""

    def __init__(
        self,
        db: Database = database,
        sessions: SessionService = session_service,
        documents: DocumentService = document_service,
        memories: MemoryService = memory_service,
        tokens: TokenCounter = token_counter,
        token_budget: int = 32_000,
    ) -> None:
        self.db = db
        self.sessions = sessions
        self.documents = documents
        self.memories = memories
        self.tokens = tokens
        self.token_budget = token_budget

    def build(
        self,
        session_id: str,
        request: ChatRequest,
        agent_role: str,
        current_message_id: str,
    ) -> ContextBuildResult:
        # 首次上传仍兼容旧前端：入库后下轮只需项目/模块和查询，不必再次传整篇正文。
        for document in request.documents:
            self.documents.ingest(
                DocumentIngestRequest(
                    project_id=request.project_id,
                    module_name=request.module_name,
                    kind=document.kind,
                    name=document.name,
                    version=document.version,
                    content=document.content,
                )
            )

        recent = self.sessions.recent_messages(
            session_id,
            agent_role=agent_role,
            exclude_message_id=current_message_id,
            limit=24,
        )
        self._refresh_summary(session_id, recent)
        summary = self.sessions.get_summary(session_id)

        history: list[ChatHistoryMessage] = []
        history_tokens = 0
        if summary:
            summary_text = f"[较早会话摘要，覆盖至消息 {summary.covered_until_sequence}]\n{summary.summary}"
            summary_text = self.tokens.trim(summary_text, 3_000)
            history.append(ChatHistoryMessage(role="assistant", content=summary_text))
            history_tokens += self.tokens.count(summary_text)
        # 近期消息从新到旧按预算选择，再恢复时间顺序。这样最接近当前问题的内容优先。
        selected_recent = []
        for message in reversed(recent):
            cost = self.tokens.count(message.content)
            if history_tokens + cost > 8_000:
                break
            selected_recent.append(message)
            history_tokens += cost
        selected_recent.reverse()
        history.extend(
            ChatHistoryMessage(
                role="user" if message.role == "user" else "assistant",
                content=message.content,
            )
            for message in selected_recent
            if message.role in {"user", "assistant"}
        )
        # 新会话迁移期若后端还没有历史，则使用旧客户端携带的历史作为一次性回退。
        if not selected_recent and request.history:
            history.extend(request.history[-12:])
            history_tokens += sum(self.tokens.count(item.content) for item in request.history[-12:])

        memories = self.memories.retrieve(
            request.project_id, request.module_name, request.message, limit=8
        )
        memory_context = [
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind,
                "content": memory.content,
                "confidence": memory.confidence,
                "authority": memory.authority,
                "sources": memory.source_refs,
            }
            for memory in memories
        ]
        memory_tokens = sum(self.tokens.count(memory.content) for memory in memories)

        retrieval = self.documents.retrieve(
            RetrievalRequest(
                project_id=request.project_id,
                module_name=request.module_name,
                query=request.message,
                top_k=14,
            )
        )
        selected_chunks = []
        document_tokens = 0
        # 给文档证据最多 18k token；先按混合分数排序，而不是按上传顺序截断。
        for chunk in retrieval.chunks:
            if document_tokens + chunk.token_count > 18_000:
                continue
            selected_chunks.append(chunk)
            document_tokens += chunk.token_count
        documents = [
            DocumentInput(
                kind=chunk.document_kind,
                name=chunk.document_name,
                version=chunk.document_version,
                content=(
                    f"[RAG source: chunk={chunk.chunk_id}; section={chunk.section}; "
                    f"score={chunk.score}]\n{chunk.content}"
                ),
            )
            for chunk in selected_chunks
        ]

        estimated = history_tokens + memory_tokens + document_tokens + self.tokens.count(request.message)
        effective = request.model_copy(update={"history": history, "documents": documents})
        sections = {
            "history": {"items": len(history), "tokens": history_tokens},
            "memory": {"items": len(memories), "tokens": memory_tokens},
            "documents": {"items": len(selected_chunks), "tokens": document_tokens},
            "query": {"tokens": self.tokens.count(request.message)},
        }
        return ContextBuildResult(
            effective_request=effective,
            message_ids=[message.message_id for message in selected_recent],
            memory_ids=[memory.memory_id for memory in memories],
            chunk_ids=[chunk.chunk_id for chunk in selected_chunks],
            memory_context=memory_context,
            estimated_tokens=estimated,
            sections=sections,
        )

    def save_manifest(
        self,
        session_id: str,
        run_id: str,
        agent_role: str,
        result: ContextBuildResult,
    ) -> ContextManifestView:
        context_id = str(uuid4())
        created_at = utc_now()
        self.db.execute(
            """INSERT INTO context_manifests
            (context_id, session_id, run_id, agent_role, token_budget, estimated_tokens,
             message_ids_json, memory_ids_json, chunk_ids_json, observation_ids_json,
             sections_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)""",
            (
                context_id,
                session_id,
                run_id,
                agent_role,
                self.token_budget,
                result.estimated_tokens,
                self.db.dumps(result.message_ids),
                self.db.dumps(result.memory_ids),
                self.db.dumps(result.chunk_ids),
                self.db.dumps(result.sections),
                created_at,
            ),
        )
        return self.get_manifest(context_id)  # type: ignore[return-value]

    def get_manifest(self, context_id: str) -> ContextManifestView | None:
        row = self.db.fetch_one(
            "SELECT * FROM context_manifests WHERE context_id = ?", (context_id,)
        )
        return self._manifest(row) if row else None

    def list_for_run(self, run_id: str) -> list[ContextManifestView]:
        rows = self.db.fetch_all(
            "SELECT * FROM context_manifests WHERE run_id = ? ORDER BY created_at", (run_id,)
        )
        return [self._manifest(row) for row in rows]

    def _refresh_summary(self, session_id: str, recent: list) -> None:
        """对近期窗口之前的消息做可追溯抽取摘要，不删除原消息。"""
        if not recent:
            return
        cutoff = recent[0].sequence - 1
        existing = self.sessions.get_summary(session_id)
        covered = existing.covered_until_sequence if existing else 0
        if cutoff <= covered:
            return
        rows = self.db.fetch_all(
            """SELECT role, content, sequence FROM conversation_messages
            WHERE session_id = ? AND sequence > ? AND sequence <= ? ORDER BY sequence""",
            (session_id, covered, cutoff),
        )
        if not rows:
            return
        old = existing.summary + "\n" if existing else ""
        additions = [
            f"- #{row['sequence']} {row['role']}: {' '.join(row['content'].split())[:240]}"
            for row in rows
        ]
        summary = self.tokens.trim(old + "\n".join(additions), 3_000)
        self.sessions.upsert_summary(session_id, summary, cutoff)

    def _manifest(self, row: dict) -> ContextManifestView:
        payload = dict(row)
        for field in ("message_ids", "memory_ids", "chunk_ids", "observation_ids", "sections"):
            payload[field] = self.db.loads(payload.pop(f"{field}_json"), {} if field == "sections" else [])
        return ContextManifestView(**payload)


context_builder_service = ContextBuilderService()
