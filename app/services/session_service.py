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
    """Persists conversation-level working role and resolves the primary Agent."""

    def __init__(self, db: Database = database) -> None:
        self.db = db

    def resolve(
        self,
        session_id: str,
        project_id: str,
        requested_role: UserRole | None = None,
    ) -> ConversationView:
        existing = self.get(session_id)
        if existing is not None:
            if existing.project_id != project_id:
                raise ValueError("Conversation belongs to a different project")
            if requested_role is not None and requested_role != existing.user_role:
                return self.set_role(session_id, requested_role)
            return existing

        role = requested_role or UserRole.DESIGN_ENGINEER
        primary_agent = PRIMARY_AGENT_BY_USER_ROLE[role]
        now = utc_now()
        self.db.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project_id, role.value, primary_agent.value, now, now),
        )
        return self.get(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> ConversationView | None:
        row = self.db.fetch_one(
            "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
        )
        return ConversationView(**row) if row else None

    def set_role(self, session_id: str, user_role: UserRole) -> ConversationView:
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
