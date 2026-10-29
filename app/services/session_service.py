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
from app.domain.models import ConversationView
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
                return self.set_role(session_id, requested_role)
            return existing

        # 兼容历史请求：没有显式角色时，默认按设计工程师工作模式运行。
        role = requested_role or UserRole.DESIGN_ENGINEER
        primary_agent = PRIMARY_AGENT_BY_USER_ROLE[role]
        now = utc_now()
        self.db.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project_id, role.value, primary_agent.value, now, now),
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


session_service = SessionService()
