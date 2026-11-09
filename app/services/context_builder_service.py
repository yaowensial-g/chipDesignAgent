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
    turn_summary_ids: list[str]
    raw_turn_ids: list[str]
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
    summary: int = 6_000
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

        # 对话历史以完整问答轮次为原子单位。当前 User 尚无最终 Assistant，天然不会
        # 出现在 completed_turns 中；exclude_message_id 再提供一道防重复保护。
        completed_turns = self.sessions.completed_turns(
            session_id,
            agent_role=agent_role,
            exclude_message_id=current_message_id,
        )
        recent_turns = completed_turns[-self.summaries.policy.recent_turns:]
        older_turns = completed_turns[:-self.summaries.policy.recent_turns][
            -self.summaries.policy.history_turns:
        ]
        oldest_recent_sequence = recent_turns[0].sequence if recent_turns else None
        completed_summaries = self.summaries.get_recent_turn_summaries(
            session_id,
            agent_role,
            before_sequence=oldest_recent_sequence,
            limit=self.summaries.policy.history_turns,
        )
        summaries_by_turn = {item.turn_id: item for item in completed_summaries}

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
        selected_messages = []
        selected_raw_turns = []
        selected_summary_rows = []

        # 最近轮次优先占用约 6000 Token，User/Assistant 两条消息要么同时入选，
        # 要么整轮排除，绝不会留下半轮上下文。
        recent_cap = min(self.budget.recent_messages, remaining)
        recent_used = 0
        for turn in reversed(recent_turns):
            cost = self.tokens.count(turn.user.content) + self.tokens.count(turn.assistant.content)
            if recent_used + cost > recent_cap:
                excluded.append({
                    "category": "raw_turn",
                    "id": turn.turn_id,
                    "reason": "近期完整问答区预算不足；整轮排除",
                    "tokens": cost,
                })
                continue
            selected_raw_turns.append(turn)
            recent_used += cost
        selected_raw_turns.reverse()
        remaining -= recent_used

        # 最近 5 轮之前只看最近 30 个完整轮次。已完成摘要优先使用；pending、
        # processing、failed 或尚未排队的轮次回退到原始问答，避免异步延迟丢信息。
        older_units = []
        for turn in older_turns:
            summary_row = summaries_by_turn.get(turn.turn_id)
            if summary_row:
                text = (
                    f"[独立轮次摘要 turn_id={turn.turn_id}; sequence={turn.sequence}; "
                    f"role={agent_role}]\n"
                    + json.dumps(summary_row.summary_json, ensure_ascii=False)
                )
                older_units.append((turn, summary_row, text, self.tokens.count(text)))
            else:
                cost = self.tokens.count(turn.user.content) + self.tokens.count(turn.assistant.content)
                older_units.append((turn, None, None, cost))

        older_cap = min(self.budget.summary, remaining)
        older_used = 0
        selected_older = []
        for unit in reversed(older_units):
            turn, summary_row, _, cost = unit
            if older_used + cost > older_cap:
                excluded.append({
                    "category": "turn_summary" if summary_row else "raw_turn_fallback",
                    "id": summary_row.id if summary_row else turn.turn_id,
                    "reason": "较早轮次区预算不足；摘要或完整回退轮次整体排除",
                    "tokens": cost,
                })
                continue
            selected_older.append(unit)
            older_used += cost
        selected_older.reverse()
        remaining -= older_used

        for turn, summary_row, text, _ in selected_older:
            if summary_row:
                selected_summary_rows.append(summary_row)
                history.append(ChatHistoryMessage(role="assistant", content=text))
            else:
                selected_messages.extend((turn.user, turn.assistant))
                history.extend((
                    ChatHistoryMessage(role="user", content=turn.user.content),
                    ChatHistoryMessage(role="assistant", content=turn.assistant.content),
                ))
        for turn in selected_raw_turns:
            selected_messages.extend((turn.user, turn.assistant))
            history.extend((
                ChatHistoryMessage(role="user", content=turn.user.content),
                ChatHistoryMessage(role="assistant", content=turn.assistant.content),
            ))

        history_tokens = recent_used + older_used
        summary_tokens = sum(unit[3] for unit in selected_older if unit[1] is not None)
        raw_turn_ids = [unit[0].turn_id for unit in selected_older if unit[1] is None]
        raw_turn_ids.extend(turn.turn_id for turn in selected_raw_turns)
        turn_summary_ids = [item.id for item in selected_summary_rows]
        # 新会话迁移期若后端还没有历史，则使用旧客户端携带的历史作为一次性回退。
        if not history and request.history:
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
            "history": {
                "items": len(history),
                "tokens": history_tokens,
                "raw_turns": len(raw_turn_ids),
                "turn_summaries": len(turn_summary_ids),
            },
            "summary": {
                "items": len(selected_summary_rows),
                "tokens": summary_tokens,
                "turn_summary_ids": turn_summary_ids,
            },
            "raw_conversation": {
                "turns": len(raw_turn_ids),
                "messages": len(selected_messages),
                "tokens": history_tokens - summary_tokens,
                "turn_ids": raw_turn_ids,
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
            "message_ids": [message.message_id for message in selected_messages],
            "turn_summary_ids": turn_summary_ids,
            "raw_turn_ids": raw_turn_ids,
            "memory_ids": [memory.memory_id for memory in selected_memories],
            "chunk_ids": [chunk.chunk_id for chunk in selected_chunks],
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return ContextBuildResult(
            effective_request=effective,
            message_ids=[message.message_id for message in selected_messages],
            turn_summary_ids=turn_summary_ids,
            raw_turn_ids=raw_turn_ids,
            memory_ids=[memory.memory_id for memory in selected_memories],
            chunk_ids=[chunk.chunk_id for chunk in selected_chunks],
            memory_context=memory_context,
            estimated_tokens=estimated,
            sections=sections,
            summary={
                "agent_role": agent_role,
                "turn_summary_ids": turn_summary_ids,
                "turn_ids": [item.turn_id for item in selected_summary_rows],
                "raw_turn_ids": raw_turn_ids,
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
             turn_summary_ids_json, raw_turn_ids_json, sections_json, summary_json,
             excluded_json, prompt_hash, context_version,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, 3, ?)""",
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
                self.db.dumps(result.turn_summary_ids),
                self.db.dumps(result.raw_turn_ids),
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
            "turn_summary_ids", "raw_turn_ids", "sections", "summary", "excluded",
        ):
            default = {} if field in {"sections", "summary"} else []
            payload[field] = self.db.loads(payload.pop(f"{field}_json"), default)
        return ContextManifestView(**payload)


context_builder_service = ContextBuilderService()
