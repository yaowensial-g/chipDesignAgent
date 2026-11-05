"""按 Token 预算组装模型上下文，并记录可审计的 Context Manifest。"""

from __future__ import annotations

import hashlib
import json
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
from app.config.model_settings import model_settings_service
from app.services.document_service import DocumentService, document_service
from app.services.conversation_summary_service import (
    ConversationSummaryService,
    conversation_summary_service,
)
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
    summary: dict[str, object]
    excluded: list[dict[str, object]]
    prompt_hash: str


@dataclass(frozen=True)
class ContextBudgetConfig:
    total: int = 32_000
    system_and_tools: int = 4_000
    current_request_and_run: int = 3_000
    recent_messages: int = 6_000
    summary: int = 2_000
    long_term_memory: int = 2_000
    documents: int = 12_000
    output_reserve: int = 3_000


class ContextBuilderService:
    """把短期历史、摘要、长期记忆和 RAG 片段组合成有界上下文。"""

    def __init__(
        self,
        db: Database = database,
        sessions: SessionService = session_service,
        documents: DocumentService = document_service,
        memories: MemoryService = memory_service,
        summaries: ConversationSummaryService = conversation_summary_service,
        tokens: TokenCounter = token_counter,
        token_budget: int = 32_000,
        budget_config: ContextBudgetConfig | None = None,
    ) -> None:
        self.db = db
        self.sessions = sessions
        self.documents = documents
        self.memories = memories
        self.summaries = summaries
        self.tokens = tokens
        self.budget = budget_config or ContextBudgetConfig(total=token_budget)
        self._uses_default_budget = budget_config is None
        self.token_budget = self.budget.total

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
        window_cutoff = recent[0].sequence - 1 if recent else 0
        self.summaries.refresh(session_id, agent_role, recent)
        summary = self.sessions.get_summary(session_id, agent_role)
        covered_until = summary.covered_until_sequence if summary else 0
        uncovered_messages = (
            self.sessions.visible_messages_between(
                session_id,
                agent_role=agent_role,
                after_sequence=covered_until,
                through_sequence=window_cutoff,
            )
            if window_cutoff > covered_until
            else []
        )
        if summary:
            recent = [
                message for message in recent
                if message.sequence > summary.covered_until_sequence
            ]

        query_tokens = self.tokens.count(request.message)
        configured_output = model_settings_service.effective().max_output_tokens
        output_reserve = (
            max(self.budget.output_reserve, min(configured_output, 8_000))
            if self._uses_default_budget
            else self.budget.output_reserve
        )
        fixed_tokens = (
            self.budget.system_and_tools
            + max(self.budget.current_request_and_run, query_tokens)
            + output_reserve
        )
        if fixed_tokens > self.token_budget:
            raise ValueError(
                f"当前请求与强制保留区需要 {fixed_tokens} tokens，超过上下文预算 {self.token_budget}"
            )
        remaining = self.token_budget - fixed_tokens
        excluded: list[dict[str, object]] = []

        history: list[ChatHistoryMessage] = []
        history_tokens = 0
        if summary:
            summary_text = (
                f"[较早会话摘要 role={agent_role}; 覆盖消息 "
                f"{summary.covered_from_sequence}-{summary.covered_until_sequence}; "
                f"mode={summary.generation_mode}]\n{summary.summary_text}"
            )
            summary_cost = self.tokens.count(summary_text)
            if summary_cost <= remaining:
                history.append(ChatHistoryMessage(role="assistant", content=summary_text))
                history_tokens += summary_cost
                remaining -= summary_cost
            else:
                excluded.append({
                    "category": "summary",
                    "id": f"{session_id}:{agent_role}",
                    "reason": "结构化摘要整体超过剩余预算；未进行末尾截断",
                    "tokens": summary_cost,
                })
        # 近期消息从新到旧按预算选择，再恢复时间顺序。这样最接近当前问题的内容优先。
        uncovered_tokens = sum(self.tokens.count(item.content) for item in uncovered_messages)
        if uncovered_tokens > remaining:
            raise ValueError(
                "会话摘要尚未成功更新，未覆盖原文超过剩余上下文预算；"
                "已保留摘要游标，请重试或提高上下文预算"
            )
        selected_recent = list(uncovered_messages)
        selected_normal = []
        recent_used = uncovered_tokens
        recent_cap = min(
            max(self.budget.recent_messages, uncovered_tokens), remaining
        )
        for message in reversed(recent):
            cost = self.tokens.count(message.content)
            if recent_used + cost > recent_cap:
                excluded.append({
                    "category": "recent_message",
                    "id": message.message_id,
                    "reason": "近期消息区预算不足",
                    "tokens": cost,
                })
                continue
            selected_normal.append(message)
            recent_used += cost
        selected_normal.reverse()
        selected_recent.extend(selected_normal)
        history_tokens += recent_used
        remaining -= recent_used
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
            for item in reversed(request.history[-12:]):
                cost = self.tokens.count(item.content)
                if cost <= remaining:
                    history.insert(0, item)
                    history_tokens += cost
                    remaining -= cost

        memories = self.memories.retrieve(
            request.project_id,
            request.module_name,
            request.message,
            limit=30,
            agent_role=agent_role,
        )
        selected_memories = []
        memory_tokens = 0
        memory_cap = min(self.budget.long_term_memory, remaining)
        for memory in memories:
            cost = self.tokens.count(memory.content)
            critical = memory.selection_reason == "critical_constraint"
            allowed = remaining if critical else memory_cap
            if memory_tokens + cost <= allowed:
                selected_memories.append(memory)
                memory_tokens += cost
            else:
                excluded.append({
                    "category": "memory",
                    "id": memory.memory_id,
                    "reason": "长期记忆区预算不足",
                    "tokens": cost,
                    "score": memory.retrieval_score,
                })
        remaining -= memory_tokens
        memory_context = [
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind,
                "content": memory.content,
                "confidence": memory.confidence,
                "authority": memory.authority,
                "sources": memory.source_refs,
                "retrieval_score": memory.retrieval_score,
                "selection_reason": memory.selection_reason,
            }
            for memory in selected_memories
        ]

        retrieval = self.documents.retrieve(
            RetrievalRequest(
                project_id=request.project_id,
                module_name=request.module_name,
                query=request.message,
                top_k=50,
            )
        )
        selected_chunks = []
        document_tokens = 0
        # 文档是最后一个大区段，可使用前面摘要/历史/记忆未消耗的额度。
        document_cap = remaining
        current_document_refs = {(item.name, item.version) for item in request.documents}
        for chunk in retrieval.chunks:
            current_request_evidence = (
                chunk.document_name, chunk.document_version
            ) in current_document_refs
            if chunk.score <= 0 and not current_request_evidence:
                excluded.append({
                    "category": "document_chunk",
                    "id": chunk.chunk_id,
                    "reason": "检索相关性为零",
                    "score": chunk.score,
                })
                continue
            if document_tokens + chunk.token_count > document_cap:
                excluded.append({
                    "category": "document_chunk",
                    "id": chunk.chunk_id,
                    "reason": "文档证据区预算不足",
                    "tokens": chunk.token_count,
                    "score": chunk.score,
                })
                continue
            selected_chunks.append(chunk)
            document_tokens += chunk.token_count
        remaining -= document_tokens
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

        estimated = fixed_tokens + history_tokens + memory_tokens + document_tokens
        if estimated > self.token_budget:
            raise RuntimeError("ContextBuilder produced an over-budget context")
        effective = request.model_copy(update={"history": history, "documents": documents})
        sections = {
            "history": {"items": len(history), "tokens": history_tokens},
            "summary": {
                "included": bool(summary and not any(item["category"] == "summary" for item in excluded)),
                "tokens": self.tokens.count(summary.summary_text) if summary else 0,
                "covered_from_sequence": summary.covered_from_sequence if summary else 0,
                "covered_until_sequence": summary.covered_until_sequence if summary else 0,
                "generation_mode": summary.generation_mode if summary else None,
            },
            "memory": {
                "items": len(selected_memories),
                "tokens": memory_tokens,
                "scores": [
                    {"memory_id": item.memory_id, "score": item.retrieval_score, "reason": item.selection_reason}
                    for item in selected_memories
                ],
            },
            "documents": {
                "items": len(selected_chunks),
                "tokens": document_tokens,
                "scores": [
                    {
                        "chunk_id": item.chunk_id,
                        "score": item.score,
                        "keyword_score": item.keyword_score,
                        "semantic_score": item.semantic_score,
                        "reason": (
                            "current_request_document"
                            if (item.document_name, item.document_version) in current_document_refs
                            else "hybrid_retrieval_rank"
                        ),
                    }
                    for item in selected_chunks
                ],
            },
            "query": {"tokens": query_tokens},
            "reserved": {
                "system_and_tools": self.budget.system_and_tools,
                "current_request_and_run": max(self.budget.current_request_and_run, query_tokens),
                "output": output_reserve,
            },
            "unused_tokens": self.token_budget - estimated,
        }
        prompt_hash = hashlib.sha256(json.dumps({
            "message": request.message,
            "message_ids": [message.message_id for message in selected_recent],
            "memory_ids": [memory.memory_id for memory in selected_memories],
            "chunk_ids": [chunk.chunk_id for chunk in selected_chunks],
            "summary_version": summary.summary_version if summary else 0,
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return ContextBuildResult(
            effective_request=effective,
            message_ids=[message.message_id for message in selected_recent],
            memory_ids=[memory.memory_id for memory in selected_memories],
            chunk_ids=[chunk.chunk_id for chunk in selected_chunks],
            memory_context=memory_context,
            estimated_tokens=estimated,
            sections=sections,
            summary={
                "agent_role": agent_role,
                "covered_from_sequence": summary.covered_from_sequence if summary else 0,
                "covered_until_sequence": summary.covered_until_sequence if summary else 0,
                "source_message_ids": summary.source_message_ids if summary else [],
                "summary_version": summary.summary_version if summary else 0,
                "generation_mode": summary.generation_mode if summary else None,
            },
            excluded=excluded,
            prompt_hash=prompt_hash,
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
             sections_json, summary_json, excluded_json, prompt_hash, context_version,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, 2, ?)""",
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
                self.db.dumps(result.summary),
                self.db.dumps(result.excluded),
                result.prompt_hash,
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

    def _manifest(self, row: dict) -> ContextManifestView:
        payload = dict(row)
        for field in (
            "message_ids", "memory_ids", "chunk_ids", "observation_ids",
            "sections", "summary", "excluded",
        ):
            default = {} if field in {"sections", "summary"} else []
            payload[field] = self.db.loads(payload.pop(f"{field}_json"), default)
        return ContextManifestView(**payload)


context_builder_service = ContextBuilderService()
