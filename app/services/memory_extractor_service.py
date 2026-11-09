"""从消息和工程事件中幂等提取 proposed 长期记忆候选。

消息是否值得长期保存由模型在一次结构化调用中判断。确定性代码只负责准备有限、
按角色过滤的上下文，校验候选形状，并把来源、作用域和生命周期交给 MemoryService。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from app.config.model_settings import model_settings_service
from app.domain.models import MemoryCreateRequest, MemoryView
from app.models.factory import provider_factory
from app.services.memory_service import (
    MemoryService,
    memory_service,
    normalize_memory_content,
    normalize_memory_key,
)
from app.services.session_service import SessionService
from app.services.token_service import TokenCounter, token_counter
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


# 前文和旧记忆只用于理解“第二种”“改成 32”等指代，不应取代当前消息成为候选依据。
# 这里同时限制条数和 Token，避免每条聊天都把长会话重新发送给提取模型。
MESSAGE_HISTORY_LIMIT = 6
MESSAGE_CONTEXT_TOKEN_LIMIT = 1_200
MESSAGE_ITEM_TOKEN_LIMIT = 320
RELATED_MEMORY_LIMIT = 6
MEMORY_ITEM_TOKEN_LIMIT = 240


class MemoryExtractorService:
    """模型只提出候选；来源校验、幂等和状态由 MemoryService 决定。"""

    def __init__(
        self,
        db: Database = database,
        memories: MemoryService = memory_service,
        sessions: SessionService | None = None,
        tokens: TokenCounter = token_counter,
    ) -> None:
        self.db = db
        self.memories = memories
        # 测试通常注入独立 Database；基于同一 DB 创建 SessionService，防止意外读取
        # 生产全局会话。生产实例也可显式注入，以便未来统一由 Composition Root 装配。
        self.sessions = sessions or SessionService(db)
        self.tokens = tokens

    def extract_from_message(
        self,
        *,
        message_id: str,
        project_id: str,
        module_name: str,
        agent_role: str,
        content: str,
        session_id: str | None = None,
        temporary_assumption: bool = False,
    ) -> list[MemoryView]:
        """让模型判断一条新消息是否包含值得长期保存的信息。

        session_id 是可选参数，保留直接调用的兼容性；缺省时从已持久化消息反查。
        temporary_assumption 用于 Run 的“按临时假设继续”，即使模型误判也不会把
        该假设按 fact/decision 入库。
        """
        extraction_key = f"conversation_message:{message_id}"
        # 在准备上下文和调用模型之前检查事件幂等，重复提交同一消息不会产生模型成本。
        if self._completed(extraction_key):
            return self._event_memories(extraction_key)
        self._start_event(
            extraction_key,
            "conversation_message",
            message_id,
            project_id,
            module_name,
            agent_role,
        )
        try:
            recent_messages, active_memories = self._message_context(
                message_id=message_id,
                session_id=session_id,
                project_id=project_id,
                module_name=module_name,
                agent_role=agent_role,
                content=content,
            )
            candidates = self._model_candidates(
                content,
                source_type="conversation_message",
                project_id=project_id,
                module_name=module_name,
                agent_role=agent_role,
                recent_messages=recent_messages,
                active_memories=active_memories,
                temporary_assumption=temporary_assumption,
            )
            created = self._store_candidates(
                extraction_key=extraction_key,
                source_type="conversation_message",
                source_id=message_id,
                project_id=project_id,
                module_name=module_name,
                agent_role=agent_role,
                candidates=candidates,
                force_assumption=temporary_assumption,
            )
            self._finish_event(extraction_key, [item.memory_id for item in created])
            return created
        except Exception as exc:
            # 记忆是旁路能力；失败写审计但不能阻断主 Agent 当前请求。
            self._fail_event(extraction_key, exc)
            return []

    def extract_from_event(
        self,
        *,
        source_type: str,
        source_id: str,
        project_id: str,
        module_name: str | None,
        agent_role: str | None,
        content: str,
    ) -> list[MemoryView]:
        """处理文档、Run、Delegate；可选消息上下文不会影响这些既有事件入口。"""
        extraction_key = f"{source_type}:{source_id}"
        if self._completed(extraction_key):
            return self._event_memories(extraction_key)
        self._start_event(
            extraction_key,
            source_type,
            source_id,
            project_id,
            module_name,
            agent_role,
        )
        try:
            # 工程事件没有会话前文，但仍提供作用域，让模型生成稳定、可复用的 key。
            candidates = self._model_candidates(
                content,
                source_type=source_type,
                project_id=project_id,
                module_name=module_name,
                agent_role=agent_role,
            )
            created = self._store_candidates(
                extraction_key=extraction_key,
                source_type=source_type,
                source_id=source_id,
                project_id=project_id,
                module_name=module_name,
                agent_role=agent_role,
                candidates=candidates,
            )
            self._finish_event(extraction_key, [item.memory_id for item in created])
            return created
        except Exception as exc:
            self._fail_event(extraction_key, exc)
            return []

    @staticmethod
    def _model_candidates(
        content: str,
        *,
        source_type: str = "event",
        project_id: str | None = None,
        module_name: str | None = None,
        agent_role: str | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
        active_memories: list[dict[str, Any]] | None = None,
        temporary_assumption: bool = False,
    ) -> list[dict[str, Any]]:
        """执行一次结构化判断；上下文字段均可选，供非消息事件继续复用。"""
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        settings = model_settings_service.effective()
        if mode in {"test", "mock", "deterministic"} or not settings.ready:
            return []
        result = provider_factory.get(settings.main_endpoint()).generate_json(
            """你是芯片设计项目的长期记忆提取器。source_type=conversation_message 时，只判断 current_content 中用户明确新增、确认或修订，且会影响后续工作的事实、工程约束、技术决策、稳定偏好或可复用经验；其他 source_type 则提取该正式文档或已完成工程事件明确给出的同类信息。典型内容包括时钟频率、复位方式、接口协议、数据位宽、FIFO 深度、背压行为和已经确认的验证结论。一条内容可以生成多个彼此独立的候选，每个候选使用稳定、可复用的 memory_key；若 current_content 是对旧约束的修订，优先复用 related_active_memories 中相同语义的 memory_key。

候选必须先在语义上去重：同一个工程属性、同一个事实只输出一条；不同措辞、同义表达和重复描述不能生成多条候选；同一属性使用统一的小写点分 memory_key，例如 uart.fifo_depth。当前内容明确修改某个属性时只输出修改后的有效结论，不同时输出旧值和新值。不同工程属性必须分别保存，不能因为来自同一句话或内容相似而合并；无法确定两条内容是否属于同一事实时不要擅自合并。

recent_visible_messages 和 related_active_memories 只能用于理解“第二种”“改成 32”等指代、识别修订，不能单独作为新候选依据；每个候选都必须能在 current_content 中找到依据。仅有提问、闲聊、讨论中的建议、未确认设想或没有新增信息时，返回 {"memories":[]}。疑问句如“UART 时钟为 50 MHz 可以吗？”不是确认事实。temporary_assumption_approval=true 表示用户只允许按临时假设继续，此时不得输出 fact 或 decision，确需保留时只能输出 assumption。

返回 JSON 对象：{"memories":[{"memory_key":"稳定键","kind":"fact|decision|experience|preference|assumption|inference","content":"简洁且自包含的内容","confidence":1-10,"importance":1-10}]}。不要输出项目、模块、角色、状态、authority 或 source；这些字段由后端赋值。""",
            {
                "source_type": source_type,
                "project_id": project_id,
                "module_name": module_name,
                "agent_role": agent_role,
                "current_content": content,
                "recent_visible_messages": recent_messages or [],
                "related_active_memories": active_memories or [],
                "temporary_assumption_approval": temporary_assumption,
            },
            operation="长期记忆候选提取",
            expected_shape='{"memories":[]}',
        )
        if not isinstance(result, dict):
            raise ValueError("memory extraction output must be an object")
        return [item for item in result.get("memories", []) if isinstance(item, dict)]

    def _message_context(
        self,
        *,
        message_id: str,
        session_id: str | None,
        project_id: str,
        module_name: str,
        agent_role: str,
        content: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """构造有限上下文，并严格复用 SessionService 的角色可见性规则。"""
        resolved_session_id = session_id
        if not resolved_session_id:
            row = self.db.fetch_one(
                "SELECT session_id FROM conversation_messages WHERE message_id = ?",
                (message_id,),
            )
            resolved_session_id = row["session_id"] if row else None

        visible = []
        if resolved_session_id:
            visible = self.sessions.recent_messages(
                resolved_session_id,
                agent_role=agent_role,
                exclude_message_id=message_id,
                limit=MESSAGE_HISTORY_LIMIT,
            )

        # 从最新消息向前选，保证有限预算优先保留最接近当前指代的内容；发送前恢复时序。
        selected_reversed: list[dict[str, Any]] = []
        used_tokens = 0
        for message in reversed(visible):
            remaining = MESSAGE_CONTEXT_TOKEN_LIMIT - used_tokens
            if remaining <= 0:
                break
            text = self.tokens.trim(
                message.content,
                min(MESSAGE_ITEM_TOKEN_LIMIT, remaining),
            )
            if not text:
                continue
            used_tokens += self.tokens.count(text)
            selected_reversed.append(
                {
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": text,
                }
            )
        recent_messages = list(reversed(selected_reversed))

        # 把有限前文加入查询，可让“第二种”一类消息找到相关的既有约束；检索结果本身
        # 仍必须是 active 且满足当前角色可见性。
        retrieval_query = "\n".join(
            [content, *(item["content"] for item in recent_messages)]
        )
        related = self.memories.retrieve(
            project_id,
            module_name,
            retrieval_query,
            limit=RELATED_MEMORY_LIMIT,
            agent_role=agent_role,
        )
        active_memories = [
            {
                "memory_id": memory.memory_id,
                "memory_key": memory.memory_key,
                "kind": memory.kind,
                "content": self.tokens.trim(memory.content, MEMORY_ITEM_TOKEN_LIMIT),
                "authority": memory.authority,
            }
            for memory in related
        ]
        return recent_messages, active_memories

    def _store_candidates(
        self,
        *,
        extraction_key: str,
        source_type: str,
        source_id: str,
        project_id: str,
        module_name: str | None,
        agent_role: str | None,
        candidates: list[dict[str, Any]],
        force_assumption: bool = False,
    ) -> list[MemoryView]:
        """校验模型候选并补齐所有后端可信字段。"""
        # 临时假设先统一降级再去重，避免 fact/assumption 两种标签在降级后变成重复项。
        prepared_candidates = []
        for candidate in candidates:
            prepared = dict(candidate)
            if force_assumption and str(prepared.get("kind")) in {"fact", "decision"}:
                prepared["kind"] = "assumption"
            prepared_candidates.append(prepared)
        candidates = self._deduplicate_candidates(prepared_candidates)

        created = []
        allowed_kinds = {
            "fact",
            "decision",
            "assumption",
            "inference",
            "preference",
            "experience",
        }
        for candidate in candidates:
            memory_key = str(candidate.get("memory_key", "")).strip()
            content = str(candidate.get("content", "")).strip()
            kind = str(candidate.get("kind", "inference"))
            if not memory_key or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,240}", memory_key):
                continue
            if not content or kind not in allowed_kinds:
                continue
            # 候选标识只依赖事件和规范化事实，不依赖模型返回顺序。
            dedup_signature = (
                f"{extraction_key}:{kind}:"
                f"{normalize_memory_key(memory_key)}:"
                f"{normalize_memory_content(content)}"
            )
            item_key = hashlib.sha256(
                dedup_signature.encode("utf-8")
            ).hexdigest()
            created.append(
                self.memories.create_or_merge_candidate(
                    MemoryCreateRequest(
                        project_id=project_id,
                        module_name=module_name,
                        scope="module" if module_name else "project",
                        memory_key=memory_key,
                        kind=kind,
                        content=content,
                        confidence=max(
                            1,
                            min(10, int(candidate.get("confidence", 5))),
                        ),
                        importance=max(
                            1,
                            min(10, int(candidate.get("importance", 5))),
                        ),
                        authority=(
                            "user_event"
                            if source_type == "conversation_message"
                            else "agent"
                        ),
                        visibility="project_shared",
                        owner_agent_role=agent_role,
                        status="proposed",
                        extraction_key=item_key,
                        source_refs=[
                            {
                                "source_type": source_type,
                                "source_id": source_id,
                            }
                        ],
                    )
                )
            )
        # 后续候选可能把前一条同 key 候选推进 needs_review；统一从数据库刷新，确保
        # 返回状态与本批次事务最终结果一致，并防止合并到同一 Memory 时重复返回 ID。
        memory_ids = list(dict.fromkeys(item.memory_id for item in created))
        return [
            memory
            for memory_id in memory_ids
            if (memory := self.memories.get(memory_id))
        ]

    @staticmethod
    def _deduplicate_candidates(
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """在单次模型结果中执行保守的精确去重。

        同 key 的不同取值会保留给 MemoryService 标记冲突；不同 kind 或不同工程属性
        也不会合并。完全重复项保留首次正文，但吸收更高 confidence/importance。
        """
        unique: list[dict[str, Any]] = []
        by_signature: dict[tuple[str, str, str], dict[str, Any]] = {}

        def score(candidate: dict[str, Any], field: str) -> int:
            try:
                return max(1, min(10, int(candidate.get(field, 5))))
            except (TypeError, ValueError):
                return 5

        for raw_candidate in candidates:
            candidate = dict(raw_candidate)
            signature = (
                normalize_memory_key(str(candidate.get("memory_key", ""))),
                str(candidate.get("kind", "inference")),
                normalize_memory_content(str(candidate.get("content", ""))),
            )
            existing = by_signature.get(signature)
            if existing is None:
                candidate["confidence"] = score(candidate, "confidence")
                candidate["importance"] = score(candidate, "importance")
                by_signature[signature] = candidate
                unique.append(candidate)
                continue
            existing["confidence"] = max(
                score(existing, "confidence"), score(candidate, "confidence")
            )
            existing["importance"] = max(
                score(existing, "importance"), score(candidate, "importance")
            )
        return unique

    def _completed(self, extraction_key: str) -> bool:
        """completed 事件可直接复用结果；failed 事件允许下一次调用重试。"""
        row = self.db.fetch_one(
            "SELECT status FROM memory_extraction_events WHERE extraction_key = ?",
            (extraction_key,),
        )
        return bool(row and row["status"] == "completed")

    def _event_memories(self, extraction_key: str) -> list[MemoryView]:
        """按照审计事件保存的 ID 恢复首次提取结果。"""
        row = self.db.fetch_one(
            "SELECT memory_ids_json FROM memory_extraction_events WHERE extraction_key = ?",
            (extraction_key,),
        )
        ids = self.db.loads(row["memory_ids_json"], []) if row else []
        return [
            memory
            for memory_id in ids
            if (memory := self.memories.get(memory_id))
        ]

    def _start_event(
        self,
        extraction_key: str,
        source_type: str,
        source_id: str,
        project_id: str,
        module_name: str | None,
        agent_role: str | None,
    ) -> None:
        """创建 processing 事件；失败重试时原子增加 attempts。"""
        now = utc_now()
        self.db.execute(
            """INSERT INTO memory_extraction_events
            (extraction_key, source_type, source_id, project_id, module_name, agent_role,
             status, attempts, memory_ids_json, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'processing', 1, '[]', NULL, ?, ?)
            ON CONFLICT(extraction_key) DO UPDATE SET status = 'processing',
                attempts = memory_extraction_events.attempts + 1, error = NULL,
                updated_at = excluded.updated_at""",
            (
                extraction_key,
                source_type,
                source_id,
                project_id,
                module_name,
                agent_role,
                now,
                now,
            ),
        )

    def _finish_event(self, extraction_key: str, memory_ids: list[str]) -> None:
        """只有候选处理完整结束后才把事件标记为 completed。"""
        self.db.execute(
            """UPDATE memory_extraction_events SET status = 'completed',
            memory_ids_json = ?, error = NULL, updated_at = ? WHERE extraction_key = ?""",
            (self.db.dumps(memory_ids), utc_now(), extraction_key),
        )

    def _fail_event(self, extraction_key: str, exc: Exception) -> None:
        """保存可重试错误，不让提取失败阻断主请求。"""
        self.db.execute(
            """UPDATE memory_extraction_events SET status = 'failed', error = ?,
            updated_at = ? WHERE extraction_key = ?""",
            (
                f"{exc.__class__.__name__}: {str(exc)[:1000]}",
                utc_now(),
                extraction_key,
            ),
        )


memory_extractor_service = MemoryExtractorService()
