"""Agent 的稳定执行契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.agents.definitions import AgentDefinition
from app.services.llm_invocation_service import llm_invocation_service


class BaseAgent(ABC):
    """封装角色 Prompt 和专业能力。"""

    definition: AgentDefinition

    invocations = llm_invocation_service

    @abstractmethod
    def execute(self, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """执行一次有边界的专业分析。"""

