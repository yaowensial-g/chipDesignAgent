"""项目长期记忆的形成、生命周期、检索与来源级失效管理。"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from uuid import uuid4

from app.domain.models import MemoryCreateRequest, MemoryPromoteRequest, MemoryView
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


PROMOTABLE_STATUSES = {"proposed", "needs_review", "stale"}


def _terms(text: str) -> set[str]:
    text = text.lower()
    ascii_terms = set(re.findall(r"[a-z_][a-z0-9_]{1,}|\d+(?:\.\d+)?", text))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", text))
    return ascii_terms | {
        chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))
    }


class MemoryService:
    """所有状态变更由确定性规则控制；模型只能提交 proposed 候选。"""

    def __init__(self, db: Database = database) -> None:
        self.db = db

    def create(self, request: MemoryCreateRequest) -> MemoryView:
        # 自动提取使用稳定 extraction_key；事件重试即使越过上层幂等检查，
        # 也只会返回首次创建的候选，不会重复写入来源和冲突记录。
        if request.extraction_key:
            existing = self.db.fetch_one(
                "SELECT memory_id FROM memories WHERE extraction_key = ?",
                (request.extraction_key,),
            )
            if existing:
                return self.get(existing["memory_id"])  # type: ignore[return-value]

        validated_sources = self._validate_sources(request.source_refs)
        memory_id = str(uuid4())
        now = utc_now()
        status = request.status
        review_reason = request.review_reason
        conflicts = self._active_conflicts(
            request.project_id, request.module_name, request.memory_key, request.content
        )
        # 模型只能提交候选。相同语义键已有不同 active 结论时必须进入复核，
        # 不能用“最新生成”作为覆盖正式工程事实的依据。
        if conflicts and status != "active":
            status = "needs_review"
            review_reason = review_reason or "与同一 memory_key 的 active 记忆内容冲突"

        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO memories
                (memory_id, project_id, module_name, scope, memory_key, kind, content,
                 confidence, importance, authority, visibility, owner_agent_role, status,
                 review_reason, extraction_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id, request.project_id, request.module_name, request.scope,
                    request.memory_key, request.kind, request.content, request.confidence,
                    request.importance, request.authority, request.visibility,
                    request.owner_agent_role, status, review_reason, request.extraction_key,
                    now, now,
                ),
            )
            self._add_sources(connection, memory_id, validated_sources)
            if status == "active":
                self._supersede_conflicts(connection, memory_id, conflicts)
        return self.get(memory_id)  # type: ignore[return-value]

    def get(self, memory_id: str) -> MemoryView | None:
        row = self.db.fetch_one("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
        return self._view(row) if row else None

    def list(
        self, project_id: str, module_name: str | None = None, status: str | None = None
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
        current = self.get(memory_id)
        if current is None:
            raise KeyError(memory_id)
        if current.status not in PROMOTABLE_STATUSES:
            raise ValueError(f"Cannot promote memory from status {current.status}")
        validated_sources = self._validate_sources(request.source_refs)
        if not validated_sources:
            raise ValueError("promotion requires a valid authoritative source")
        conflicts = self._active_conflicts(
            current.project_id, current.module_name, current.memory_key, current.content,
            exclude_memory_id=memory_id,
        )
        with self.db.transaction() as connection:
            self._add_sources(connection, memory_id, validated_sources)
            connection.execute(
                """UPDATE memories SET status = 'active', authority = ?,
                review_reason = NULL, updated_at = ? WHERE memory_id = ?""",
                (request.authority, utc_now(), memory_id),
            )
            self._supersede_conflicts(connection, memory_id, conflicts)
        return self.get(memory_id)  # type: ignore[return-value]

    def retrieve(
        self,
        project_id: str,
        module_name: str,
        query: str,
        limit: int = 8,
        *,
        agent_role: str | None = None,
        shared_only: bool = False,
    ) -> list[MemoryView]:
        """先做权限/生命周期/来源过滤，再按相关性和重要性排序。"""
        # 第一阶段完全由 SQL 和后端角色规则过滤，模型无法请求越权或失效记忆。
        conditions = [
            "m.project_id = ?", "(m.module_name IS NULL OR m.module_name = ?)",
            "m.status = 'active'",
        ]
        parameters: list[object] = [project_id, module_name]
        if shared_only:
            conditions.append("m.visibility = 'project_shared'")
        elif agent_role:
            conditions.append(
                "(m.visibility = 'project_shared' OR (m.visibility = 'role_private' AND m.owner_agent_role = ?))"
            )
            parameters.append(agent_role)
        conditions.append(
            """(m.kind NOT IN ('fact','decision') OR EXISTS (
                SELECT 1 FROM memory_sources s
                WHERE s.memory_id = m.memory_id AND s.status = 'active'))"""
        )
        rows = self.db.fetch_all(
            f"SELECT m.* FROM memories m WHERE {' AND '.join(conditions)}",
            tuple(parameters),
        )
        query_terms = _terms(query)
        ranked: list[MemoryView] = []
        # 第二阶段只负责相关性排序；importance>=9 的事实/决策作为关键约束，
        # 即使用户当前措辞没有复述完整术语，也可以保留为少量强制候选。
        for row in rows:
            item = self._view(row)
            item_terms = _terms(item.content + " " + (item.memory_key or ""))
            lexical = len(query_terms & item_terms) / max(1, len(query_terms | item_terms))
            scope_score = 1.0 if item.module_name == module_name else 0.65
            authority_score = 1.0 if item.authority.lower() not in {"agent", "inference"} else 0.35
            score = (
                lexical * 0.55 + (item.importance / 10) * 0.2
                + (item.confidence / 10) * 0.1 + authority_score * 0.1
                + scope_score * 0.05
            )
            mandatory = item.importance >= 9 and item.kind in {"fact", "decision"}
            if not mandatory and lexical < 0.04:
                continue
            ranked.append(item.model_copy(update={
                "retrieval_score": round(score, 6),
                "selection_reason": "critical_constraint" if mandatory else "query_relevance",
            }))
        ranked.sort(
            key=lambda item: (
                item.selection_reason == "critical_constraint",
                item.retrieval_score or 0, item.importance, item.updated_at,
            ),
            reverse=True,
        )
        return ranked[: max(0, min(limit, 30))]

    def invalidate_sources(
        self,
        source_ids: list[str],
        *,
        connection: sqlite3.Connection | None = None,
        reason: str = "source_superseded",
    ) -> int:
        """只失效被替代的来源；按剩余独立来源决定记忆是否仍可用。"""
        if not source_ids:
            return 0

        def apply(conn: sqlite3.Connection) -> int:
            placeholders = ",".join("?" for _ in source_ids)
            rows = conn.execute(
                f"SELECT DISTINCT memory_id FROM memory_sources WHERE source_id IN ({placeholders}) AND status = 'active'",
                tuple(source_ids),
            ).fetchall()
            now = utc_now()
            conn.execute(
                f"""UPDATE memory_sources SET status = 'invalidated', invalidated_at = ?,
                invalidation_reason = ? WHERE source_id IN ({placeholders}) AND status = 'active'""",
                (now, reason, *source_ids),
            )
            for row in rows:
                memory_id = row[0]
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ? AND status = 'active'",
                    (memory_id,),
                ).fetchone()[0]
                if remaining:
                    conn.execute(
                        """UPDATE memories SET review_reason = ?, updated_at = ?
                        WHERE memory_id = ? AND status = 'active'""",
                        ("部分来源已被替代，仍有独立有效来源", now, memory_id),
                    )
                else:
                    conn.execute(
                        """UPDATE memories SET status = 'stale', review_reason = ?, updated_at = ?
                        WHERE memory_id = ? AND status = 'active'""",
                        ("全部有效来源已被替代，需要复核", now, memory_id),
                    )
            return len(rows)

        if connection is not None:
            return apply(connection)
        with self.db.transaction() as conn:
            return apply(conn)

    def _active_conflicts(
        self,
        project_id: str,
        module_name: str | None,
        memory_key: str | None,
        content: str,
        *,
        exclude_memory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not memory_key:
            return []
        params: list[object] = [project_id, module_name, memory_key]
        condition = ""
        if exclude_memory_id:
            condition = " AND memory_id <> ?"
            params.append(exclude_memory_id)
        rows = self.db.fetch_all(
            """SELECT memory_id, content FROM memories
            WHERE project_id = ? AND module_name IS ? AND memory_key = ? AND status = 'active'"""
            + condition,
            tuple(params),
        )
        normalized = " ".join(content.lower().split())
        return [row for row in rows if " ".join(row["content"].lower().split()) != normalized]

    @staticmethod
    def _supersede_conflicts(
        connection: sqlite3.Connection,
        new_memory_id: str,
        conflicts: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        for conflict in conflicts:
            old_id = conflict["memory_id"]
            connection.execute(
                """UPDATE memories SET status = 'superseded', review_reason = ?, updated_at = ?
                WHERE memory_id = ? AND status = 'active'""",
                (f"被记忆 {new_memory_id} 替代", now, old_id),
            )
            connection.execute(
                """INSERT INTO memory_relations
                (relation_id, from_memory_id, to_memory_id, relation_type, created_at)
                VALUES (?, ?, ?, 'supersedes', ?)""",
                (str(uuid4()), new_memory_id, old_id, now),
            )

    def _validate_sources(self, source_refs: list[dict]) -> list[dict]:
        validated = []
        lookups = {
            "conversation_message": ("conversation_messages", "message_id", None),
            "user_confirmation": ("conversation_messages", "message_id", "role = 'user'"),
            "document_version": ("document_versions", "version_id", None),
            "document_chunk": ("document_chunks", "chunk_id", None),
            "run": ("runs", "run_id", "status = 'completed'"),
            "delegation": ("delegation_tasks", "task_id", "status = 'completed'"),
            "patch_approval": ("patches", "patch_id", "status = 'applied'"),
        }
        for source in source_refs:
            source_id = str(source.get("source_id") or source.get("id") or "").strip()
            source_type = str(source.get("source_type") or "unknown")
            if not source_id:
                continue
            lookup = lookups.get(source_type)
            if lookup:
                table, column, authority_condition = lookup
                extra = f" AND {authority_condition}" if authority_condition else ""
                if not self.db.fetch_one(
                    f"SELECT {column} FROM {table} WHERE {column} = ?{extra}", (source_id,)
                ):
                    raise ValueError(
                        f"memory source does not exist or is not authoritative: {source_type}/{source_id}"
                    )
            elif source_type != "external":
                raise ValueError(f"unsupported memory source type: {source_type}")
            validated.append({**source, "source_type": source_type, "source_id": source_id})
        return validated

    def _add_sources(
        self, connection: sqlite3.Connection, memory_id: str, source_refs: list[dict]
    ) -> None:
        for source in source_refs:
            duplicate = connection.execute(
                """SELECT 1 FROM memory_sources
                WHERE memory_id = ? AND source_type = ? AND source_id = ? AND status = 'active'""",
                (memory_id, source["source_type"], source["source_id"]),
            ).fetchone()
            if duplicate:
                continue
            connection.execute(
                """INSERT INTO memory_sources
                (source_ref_id, memory_id, source_type, source_id, source_version,
                 location, content_hash, source_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (
                    str(uuid4()), memory_id, source["source_type"], source["source_id"],
                    source.get("source_version"), source.get("location"),
                    source.get("content_hash"), self.db.dumps(source), utc_now(),
                ),
            )

    def _view(self, row: dict) -> MemoryView:
        sources = self.db.fetch_all(
            """SELECT source_json, status, invalidated_at, invalidation_reason
            FROM memory_sources WHERE memory_id = ? ORDER BY created_at""",
            (row["memory_id"],),
        )
        source_refs = []
        for source in sources:
            payload = self.db.loads(source["source_json"], {})
            payload.update({
                "status": source["status"],
                "invalidated_at": source["invalidated_at"],
                "invalidation_reason": source["invalidation_reason"],
            })
            source_refs.append(payload)
        return MemoryView(**row, source_refs=source_refs)


memory_service = MemoryService()
