from uuid import uuid4

from app.agents.runtime import runtime
from app.domain.models import ChatRequest, ChatResponse
from app.services.run_service import run_service


class ConversationService:
    """Routes a conversation turn to chat or a user-requested engineering Run."""

    async def handle(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or str(uuid4())
        request_payload = request.model_dump(mode="json")
        decision = runtime.route_conversation(request_payload)
        route = decision["route"]
        reason = decision.get("reason")

        if route == "chat":
            return ChatResponse(
                session_id=session_id,
                assistant_message=runtime.chat(request_payload),
                route="chat",
                route_reason=reason,
            )
        run, task, message = await run_service.start_from_chat(
            session_id, request, route, reason or "编排 Agent 判定"
        )
        return ChatResponse(
            session_id=session_id,
            assistant_message=message,
            route=route,
            route_reason=reason,
            task=task,
            run=run,
        )


conversation_service = ConversationService()

