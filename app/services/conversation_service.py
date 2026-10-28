from app.domain.models import ChatRequest, ChatResponse
from app.services.orchestrator_service import orchestrator_service


class ConversationService:
    """Compatibility facade over the unified dynamic Orchestrator."""

    async def handle(self, request: ChatRequest) -> ChatResponse:
        return await orchestrator_service.handle(request)


conversation_service = ConversationService()

