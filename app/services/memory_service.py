"""项目长期记忆的形成、提升、检索与失效管理。"""

from __future__ import annotations

import re
from uuid import uuid4

from app.domain.models import MemoryCreateRequest, MemoryPromoteRequest, MemoryView
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


def _terms(text: str) -> set[str]:
    text = text.lower()
    ascii_terms = set(re.findall(r"[a-z_][a-z0-9_]{1,}|\d+", text))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", text))
    return ascii_terms | {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}


class MemoryService:
    """长期记忆只接受显式形成动作；Agent 普通输出不会自动写入 active 事实。"""

    def __init__(self, db: Database = database) -> None:
        self.db = db

    def create(self, request: MemoryCreateRequest) -> MemoryView:
        memory_id = str(uuid4())
        now = utc_now()
        self.db.execute(
            """INSERT INTO memories
            (memory_id, project_id, module_name, scope, kind, content, confidence,
             authority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory_id,
                request.project_id,
                request.module_name,
                request.scope,
                request.kind,
                request.content,
                request.confidence,
                request.authority,
                request.status,
                now,
                now,
            ),
        )
        self._add_sources(memory_id, request.source_refs)
        return self.get(memory_id)  # type: ignore[return-value]

    def get(self, memory_id: str) -> MemoryView | None:
        row = self.db.fetch_one("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
        return self._view(row) if row else None

    def list(
        self,
        project_id: str,
        module_name: str | None = None,
        status: str | None = None,
    ) -> list[MemoryView]:
        conditions = ["project_id = ?"]
        parameters: list[object] = [project_id]
        if module_name:
            conditions.append("(module_name IS NULL OR module_name = ?)")
            parameters.append(module_name)
        if status:
            conditions.append("status = ?")
            parameters.append(status)
        rows = self.db.fetch_all(
            f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY updated_at DESC",
            tuple(parameters),
        )
        return [self._view(row) for row in rows]

    def promote(self, memory_id: str, request: MemoryPromoteRequest) -> MemoryView:
        """用户/问题单/正式文档确认后，才把 proposed 记忆提升为 active。"""
        if self.get(memory_id) is None:
            raise KeyError(memory_id)
        self._add_sources(memory_id, request.source_refs)
        self.db.execute(
            """UPDATE memories SET status = 'active', authority = ?, updated_at = ?
            WHERE memory_id = ?""",
            (request.authority, utc_now(), memory_id),
        )
        return self.get(memory_id)  # type: ignore[return-value]

    def retrieve(
        self, project_id: str, module_name: str, query: str, limit: int = 8
    ) -> list[MemoryView]:
        """检索有效长期记忆；stale/superseded 永远不会默认进入模型上下文。"""
        candidates = self.list(project_id, module_name, status="active")
        query_terms = _terms(query)
        ranked = sorted(
            candidates,
            key=lambda item: (
                len(query_terms & _terms(item.content)) / max(1, len(query_terms)),
                item.confidence,
                item.updated_at,
            ),
            reverse=True,
        )
        # 没有关键词交集时仍保留少量高置信项目事实，避免“项目时钟频率”等稳定约束丢失。
        return ranked[: max(1, min(limit, 30))]

    def invalidate_sources(self, source_ids: list[str]) -> int:
        """源文档版本被替代时，将其推导记忆标 stale，而不是删除历史。"""
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.db.fetch_all(
            f"SELECT DISTINCT memory_id FROM memory_sources WHERE source_id IN ({placeholders})",
            tuple(source_ids),
        )
        for row in rows:
            self.db.execute(
                "UPDATE memories SET status = 'stale', updated_at = ? WHERE memory_id = ?",
                (utc_now(), row["memory_id"]),
            )
        return len(rows)

    def _add_sources(self, memory_id: str, source_refs: list[dict]) -> None:
        for source in source_refs:
            source_id = str(source.get("source_id") or source.get("id") or "").strip()
            if not source_id:
                # 来源契约允许外部扩展字段，但必须至少有稳定 ID 才可追踪。
                continue
            self.db.execute(
                """INSERT INTO memory_sources
                (source_ref_id, memory_id, source_type, source_id, source_version,
                 location, content_hash, source_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    memory_id,
                    str(source.get("source_type") or "unknown"),
                    source_id,
                    source.get("source_version"),
                    source.get("location"),
                    source.get("content_hash"),
                    self.db.dumps(source),
                    utc_now(),
                ),
            )

    def _view(self, row: dict) -> MemoryView:
        sources = self.db.fetch_all(
            "SELECT source_json FROM memory_sources WHERE memory_id = ? ORDER BY created_at",
            (row["memory_id"],),
        )
        return MemoryView(
            **row,
            source_refs=[self.db.loads(source["source_json"], {}) for source in sources],
        )


memory_service = MemoryService()
