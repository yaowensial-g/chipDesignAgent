"""从高价值工程事件中幂等提取 proposed 长期记忆候选。"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from app.config.model_settings import model_settings_service
from app.domain.models import MemoryCreateRequest, MemoryView
from app.models.factory import provider_factory
from app.services.memory_service import MemoryService, memory_service
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


TRIGGER_PATTERN = re.compile(
    r"确定为|确认|必须|约束|后续.*(?:按照|采用)|shall|must|constraint|approved",
    re.IGNORECASE,
)
CLOCK_PATTERN = re.compile(
    r"(?P<module>[A-Za-z_][A-Za-z0-9_]*)\s*(?:系统)?(?:clock|时钟)\s*(?:=|为|确定为)?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[GMk]?Hz)",
    re.IGNORECASE,
)


class MemoryExtractorService:
    """模型只提出候选；来源校验、幂等和状态由 MemoryService 决定。"""

    def __init__(
        self,
        db: Database = database,
        memories: MemoryService = memory_service,
    ) -> None:
        self.db = db
        self.memories = memories

    def extract_from_message(
        self,
        *,
        message_id: str,
        project_id: str,
        module_name: str,
        agent_role: str,
        content: str,
    ) -> list[MemoryView]:
        if not TRIGGER_PATTERN.search(content) and not CLOCK_PATTERN.search(content):
            return []
        extraction_key = f"conversation_message:{message_id}"
        if self._completed(extraction_key):
            return self._event_memories(extraction_key)
        self._start_event(
            extraction_key, "conversation_message", message_id,
            project_id, module_name, agent_role,
        )
        try:
            candidates = self._clock_candidates(content, module_name)
            if not candidates:
                candidates = self._model_candidates(content)
            created = self._store_candidates(
                extraction_key=extraction_key,
                source_type="conversation_message",
                source_id=message_id,
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
        """处理文档、审批、Run、Delegate；无模型时只记录已检查，不伪造结论。"""
        extraction_key = f"{source_type}:{source_id}"
        if self._completed(extraction_key):
            return self._event_memories(extraction_key)
        self._start_event(
            extraction_key, source_type, source_id,
            project_id, module_name, agent_role,
        )
        try:
            candidates = self._model_candidates(content)
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
    def _clock_candidates(content: str, fallback_module: str) -> list[dict[str, Any]]:
        candidates = []
        for match in CLOCK_PATTERN.finditer(content):
            module = match.group("module") or fallback_module
            value = match.group("value")
            unit = match.group("unit")
            candidates.append({
                "memory_key": f"{module.lower()}.clock_frequency",
                "kind": "fact",
                "content": f"{module.upper()} 系统时钟为 {value} {unit}",
                "confidence": 9,
                "importance": 9,
            })
        return candidates

    @staticmethod
    def _model_candidates(content: str) -> list[dict[str, Any]]:
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        settings = model_settings_service.effective()
        if mode in {"test", "mock", "deterministic"} or not settings.ready:
            return []
        result = provider_factory.get(settings.main_endpoint()).generate_json(
            """从工程事件中提取可跨会话复用的长期记忆候选。仅提取明确约束、用户确认的技术决策、正式事实或可复用经验。不要把建议或模型推断写成事实。返回 {\"memories\":[{\"memory_key\":稳定键,\"kind\":fact|decision|experience|preference|assumption|inference,\"content\":简洁内容,\"confidence\":1-10,\"importance\":1-10}]}。没有合适内容时返回空数组。""",
            {"event_content": content},
            operation="长期记忆候选提取",
            expected_shape='{"memories":[]}',
        )
        if not isinstance(result, dict):
            raise ValueError("memory extraction output must be an object")
        return [item for item in result.get("memories", []) if isinstance(item, dict)]

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
    ) -> list[MemoryView]:
        created = []
        allowed_kinds = {
            "fact", "decision", "assumption", "inference", "preference", "experience"
        }
        for index, candidate in enumerate(candidates):
            memory_key = str(candidate.get("memory_key", "")).strip()
            content = str(candidate.get("content", "")).strip()
            kind = str(candidate.get("kind", "inference"))
            if not memory_key or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,240}", memory_key):
                continue
            if not content or kind not in allowed_kinds:
                continue
            item_key = hashlib.sha256(
                f"{extraction_key}:{index}:{memory_key}:{content}".encode("utf-8")
            ).hexdigest()
            created.append(self.memories.create(MemoryCreateRequest(
                project_id=project_id,
                module_name=module_name,
                scope="module" if module_name else "project",
                memory_key=memory_key,
                kind=kind,
                content=content,
                confidence=max(1, min(10, int(candidate.get("confidence", 5)))),
                importance=max(1, min(10, int(candidate.get("importance", 5)))),
                authority="user_event" if source_type == "conversation_message" else "agent",
                visibility="project_shared",
                owner_agent_role=agent_role,
                status="proposed",
                extraction_key=item_key,
                source_refs=[{"source_type": source_type, "source_id": source_id}],
            )))
        return created

    def _completed(self, extraction_key: str) -> bool:
        row = self.db.fetch_one(
            "SELECT status FROM memory_extraction_events WHERE extraction_key = ?",
            (extraction_key,),
        )
        return bool(row and row["status"] == "completed")

    def _event_memories(self, extraction_key: str) -> list[MemoryView]:
        row = self.db.fetch_one(
            "SELECT memory_ids_json FROM memory_extraction_events WHERE extraction_key = ?",
            (extraction_key,),
        )
        ids = self.db.loads(row["memory_ids_json"], []) if row else []
        return [memory for memory_id in ids if (memory := self.memories.get(memory_id))]

    def _start_event(
        self, extraction_key: str, source_type: str, source_id: str,
        project_id: str, module_name: str | None, agent_role: str | None,
    ) -> None:
        now = utc_now()
        self.db.execute(
            """INSERT INTO memory_extraction_events
            (extraction_key, source_type, source_id, project_id, module_name, agent_role,
             status, attempts, memory_ids_json, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'processing', 1, '[]', NULL, ?, ?)
            ON CONFLICT(extraction_key) DO UPDATE SET status = 'processing',
                attempts = memory_extraction_events.attempts + 1, error = NULL,
                updated_at = excluded.updated_at""",
            (extraction_key, source_type, source_id, project_id, module_name, agent_role, now, now),
        )

    def _finish_event(self, extraction_key: str, memory_ids: list[str]) -> None:
        self.db.execute(
            """UPDATE memory_extraction_events SET status = 'completed',
            memory_ids_json = ?, error = NULL, updated_at = ? WHERE extraction_key = ?""",
            (self.db.dumps(memory_ids), utc_now(), extraction_key),
        )

    def _fail_event(self, extraction_key: str, exc: Exception) -> None:
        self.db.execute(
            """UPDATE memory_extraction_events SET status = 'failed', error = ?,
            updated_at = ? WHERE extraction_key = ?""",
            (f"{exc.__class__.__name__}: {str(exc)[:1000]}", utc_now(), extraction_key),
        )


memory_extractor_service = MemoryExtractorService()
