"""会话角色服务。

这个模块解决的是“当前这轮对话到底由哪个生产角色负责”这一问题。
它不保存聊天正文，只保存稳定的会话级元数据：用户工作角色以及映射出的主 Agent。

为什么单独做成 Service，而不是直接写在 API 路由里：
1. 网页、测试或未来 CLI 都可以复用同一套角色解析规则；
2. 角色变化需要持久化，后端重启后不能悄悄恢复成默认角色；
3. 主 Agent 身份会影响 Tool 权限，因此必须由后端统一计算，不能只相信前端展示。
"""

from __future__ import annotations

from app.domain.enums import AgentRole, UserRole
from uuid import uuid4

from app.domain.models import (
    ConversationMessageView,
    ConversationMessagesPage,
    ConversationSummaryView,
    ConversationView,
)
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


PRIMARY_AGENT_BY_USER_ROLE = {
    UserRole.DESIGN_ENGINEER: AgentRole.DESIGN,
    UserRole.VERIFICATION_ENGINEER: AgentRole.VERIFICATION,
}


class SessionService:
    """持久化会话工作角色，并解析该会话的主 Agent。"""

    def __init__(self, db: Database = database) -> None:
        self.db = db

    def resolve(
        self,
        session_id: str,
        project_id: str,
        requested_role: UserRole | None = None,
        module_name: str = "",
    ) -> ConversationView:
        """读取或创建会话，并在用户明确要求时切换工作角色。

        参数说明：
        - ``session_id`` 是一段连续对话的稳定标识；
        - ``project_id`` 用于阻止同一个会话被错误复用到另一个项目；
        - ``requested_role`` 是本次请求显式携带的工作角色，可以为空。

        已存在会话优先使用数据库中的角色。只有请求明确携带了不同角色时，
        才调用 ``set_role`` 更新。这样旧版客户端不传 ``user_role`` 时也能继续工作。
        """
        existing = self.get(session_id)
        if existing is not None:
            # 会话与项目绑定。若允许跨项目复用，旧项目的文档、Run 和结论可能被
            # 错误地带入新项目，所以这里必须在业务层拒绝，而不是静默覆盖。
            if existing.project_id != project_id:
                raise ValueError("Conversation belongs to a different project")
            if requested_role is not None and requested_role != existing.user_role:
                existing = self.set_role(session_id, requested_role)
            if module_name and not existing.module_name:
                self.db.execute(
                    "UPDATE conversations SET module_name = ?, updated_at = ? WHERE session_id = ?",
                    (module_name, utc_now(), session_id),
                )
                existing = self.get(session_id)  # type: ignore[assignment]
            elif module_name and existing.module_name != module_name:
                raise ValueError("Conversation belongs to a different module")
            return existing

        # 兼容历史请求：没有显式角色时，默认按设计工程师工作模式运行。
        role = requested_role or UserRole.DESIGN_ENGINEER
        primary_agent = PRIMARY_AGENT_BY_USER_ROLE[role]
        now = utc_now()
        self.db.execute(
            """INSERT INTO conversations
            (session_id, project_id, module_name, title, user_role, primary_agent,
             last_message_preview, message_count, active_run_id, created_at, updated_at)
            VALUES (?, ?, ?, '新会话', ?, ?, '', 0, NULL, ?, ?)""",
            (session_id, project_id, module_name, role.value, primary_agent.value, now, now),
        )
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> ConversationView | None:
        """按会话 ID 查询角色元数据；不存在时返回 ``None``。"""
        row = self.db.fetch_one(
            "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
        )
        return ConversationView(**row) if row else None

    def set_role(self, session_id: str, user_role: UserRole) -> ConversationView:
        """切换会话角色，并原子更新对应的主 Agent。

        注意：本方法只更新会话元数据。旧角色聊天历史的隔离由前端和
        ``OrchestratorService`` 双重处理，避免把 UI 行为误当成安全边界。
        """
        if self.get(session_id) is None:
            raise KeyError(session_id)
        primary_agent = PRIMARY_AGENT_BY_USER_ROLE[user_role]
        self.db.execute(
            """UPDATE conversations SET user_role = ?, primary_agent = ?, updated_at = ?
            WHERE session_id = ?""",
            (user_role.value, primary_agent.value, utc_now(), session_id),
        )
        return self.get(session_id)  # type: ignore[return-value]

    def list(self, project_id: str | None = None) -> list[ConversationView]:
        """按最近活动倒序列出会话，供前端刷新页面后恢复历史。"""
        if project_id:
            rows = self.db.fetch_all(
                "SELECT * FROM conversations WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            )
        else:
            rows = self.db.fetch_all("SELECT * FROM conversations ORDER BY updated_at DESC")
        return [ConversationView(**row) for row in rows]

    def set_active_run(self, session_id: str, run_id: str | None) -> None:
        """记录会话当前 Run，前端恢复会话后可以继续展示任务状态。"""
        self.db.execute(
            "UPDATE conversations SET active_run_id = ?, updated_at = ? WHERE session_id = ?",
            (run_id, utc_now(), session_id),
        )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        agent_role: str | None = None,
        message_type: str = "chat",
        run_id: str | None = None,
        delegation_task_id: str | None = None,
        metadata: dict | None = None,
    ) -> ConversationMessageView:
        """追加完整消息，并同步会话列表所需的标题、预览和计数。"""
        if self.get(session_id) is None:
            raise KeyError(session_id)
        message_id = str(uuid4())
        now = utc_now()
        # sequence 在 SQL 内计算；唯一索引会阻止极端并发下出现重复游标。
        self.db.execute(
            """INSERT INTO conversation_messages
            (message_id, session_id, sequence, role, agent_role, content, message_type,
             run_id, delegation_task_id, metadata_json, created_at)
            VALUES (?, ?,
                COALESCE((SELECT MAX(sequence) + 1 FROM conversation_messages WHERE session_id = ?), 1),
                ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                session_id,
                session_id,
                role,
                agent_role,
                content,
                message_type,
                run_id,
                delegation_task_id,
                self.db.dumps(metadata or {}),
                now,
            ),
        )
        preview = " ".join(content.strip().split())[:120]
        conversation = self.get(session_id)
        title = conversation.title if conversation else "新会话"
        if role == "user" and title == "新会话":
            title = preview[:40] or title
        self.db.execute(
            """UPDATE conversations
            SET title = ?, last_message_preview = ?, message_count = message_count + 1,
                updated_at = ? WHERE session_id = ?""",
            (title, preview, now, session_id),
        )
        return self.get_message(message_id)  # type: ignore[return-value]

    def get_message(self, message_id: str) -> ConversationMessageView | None:
        row = self.db.fetch_one(
            "SELECT * FROM conversation_messages WHERE message_id = ?", (message_id,)
        )
        return self._message_from_row(row) if row else None

    def messages(
        self, session_id: str, *, before: int | None = None, limit: int = 50
    ) -> ConversationMessagesPage:
        """分页读取历史；数据库倒序取最近 N 条，返回前恢复为时间正序。"""
        if self.get(session_id) is None:
            raise KeyError(session_id)
        bounded_limit = max(1, min(limit, 200))
        if before is None:
            rows = self.db.fetch_all(
                """SELECT * FROM conversation_messages WHERE session_id = ?
                ORDER BY sequence DESC LIMIT ?""",
                (session_id, bounded_limit + 1),
            )
        else:
            rows = self.db.fetch_all(
                """SELECT * FROM conversation_messages
                WHERE session_id = ? AND sequence < ? ORDER BY sequence DESC LIMIT ?""",
                (session_id, before, bounded_limit + 1),
            )
        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        items = [self._message_from_row(row) for row in reversed(rows)]
        return ConversationMessagesPage(
            items=items,
            next_before=items[0].sequence if has_more and items else None,
        )

    def recent_messages(
        self,
        session_id: str,
        *,
        agent_role: str | None,
        exclude_message_id: str | None = None,
        limit: int = 30,
    ) -> list[ConversationMessageView]:
        """读取短期上下文，并根据 agent_role 隔离角色私有历史。"""
        parameters: list[object] = [session_id]
        conditions = ["session_id = ?"]
        if exclude_message_id:
            conditions.append("message_id <> ?")
            parameters.append(exclude_message_id)
        if agent_role:
            conditions.append("(agent_role IS NULL OR agent_role = ?)")
            parameters.append(agent_role)
        parameters.append(max(1, min(limit, 100)))
        rows = self.db.fetch_all(
            f"""SELECT * FROM conversation_messages WHERE {' AND '.join(conditions)}
            ORDER BY sequence DESC LIMIT ?""",
            tuple(parameters),
        )
        return [self._message_from_row(row) for row in reversed(rows)]

    def get_summary(self, session_id: str) -> ConversationSummaryView | None:
        row = self.db.fetch_one(
            "SELECT * FROM conversation_summaries WHERE session_id = ?", (session_id,)
        )
        return ConversationSummaryView(**row) if row else None

    def upsert_summary(self, session_id: str, summary: str, covered_until: int) -> None:
        self.db.execute(
            """INSERT INTO conversation_summaries
            (session_id, summary, covered_until_sequence, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET summary = excluded.summary,
                covered_until_sequence = excluded.covered_until_sequence,
                updated_at = excluded.updated_at""",
            (session_id, summary, covered_until, utc_now()),
        )

    def _message_from_row(self, row: dict) -> ConversationMessageView:
        payload = dict(row)
        payload["metadata"] = self.db.loads(payload.pop("metadata_json", None), {})
        return ConversationMessageView(**payload)


session_service = SessionService()
