"""专业 Agent 的注册、发现与统一调用。"""

from __future__ import annotations

import threading
from typing import Any

from app.agents.base import BaseAgent
from app.agents.specialists import ArchitectureAgent, DesignAgent, VerificationAgent


class AgentRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent, *, replace: bool = False) -> None:
        agent_id = agent.definition.agent_id
        with self._lock:
            if agent_id in self._agents and not replace:
                raise ValueError(f"Agent already registered: {agent_id}")
            self._agents[agent_id] = agent

    def get(self, agent_id: str) -> BaseAgent:
        with self._lock:
            try:
                return self._agents[agent_id]
            except KeyError as exc:
                raise KeyError(f"Unknown agent: {agent_id}") from exc

    def list(self) -> tuple[BaseAgent, ...]:
        with self._lock:
            return tuple(self._agents.values())

    def invoke(
        self,
        agent_id: str,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return self.get(agent_id).execute(request, context)

    def call(self, agent_id: str, capability: str, *args, **kwargs):
        """调用 Agent 的公开专业能力。"""
        if capability.startswith("_"):
            raise ValueError("Private agent capabilities cannot be invoked")
        method = getattr(self.get(agent_id), capability, None)
        if not callable(method):
            raise KeyError(f"Agent {agent_id} has no capability: {capability}")
        return method(*args, **kwargs)


agent_registry = AgentRegistry()
agent_registry.register(ArchitectureAgent())
agent_registry.register(DesignAgent())
agent_registry.register(VerificationAgent())
