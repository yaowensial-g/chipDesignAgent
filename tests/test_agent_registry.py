import pytest

from app.agents.base import BaseAgent
from app.agents.definitions import AgentDefinition
from app.agents.registry import AgentRegistry, agent_registry


class _Agent(BaseAgent):
    definition = AgentDefinition(
        agent_id="test_agent",
        display_name="Test Agent",
        prompt_version="v1",
        read_only=True,
        purpose="test",
    )

    def execute(self, request, context):
        return {"summary": request["message"], "context": context}


def test_default_registry_discovers_specialist_agents() -> None:
    ids = {agent.definition.agent_id for agent in agent_registry.list()}
    assert {"architecture_advisor", "design", "verification"} <= ids


def test_registry_invokes_registered_agent_and_rejects_duplicates() -> None:
    registry = AgentRegistry()
    registry.register(_Agent())
    assert registry.invoke("test_agent", {"message": "ok"}, {"scope": "small"}) == {
        "summary": "ok",
        "context": {"scope": "small"},
    }
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_Agent())
