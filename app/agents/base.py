"""Agent 的稳定执行契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.agents.definitions import AgentDefinition
from app.config.model_settings import model_settings_service
from app.models.factory import provider_factory
from app.models.provider import OpenAIModelProvider


class BaseAgent(ABC):
    """封装角色 Prompt、模型端点选择和专业能力。"""

    definition: AgentDefinition

    def provider(self) -> OpenAIModelProvider:
        return provider_factory.get(model_settings_service.effective().main_endpoint())

    @abstractmethod
    def execute(self, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """执行一次有边界的专业分析。"""

