from dataclasses import dataclass
from typing import Callable

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ToolDefinitionView


ToolExecutor = Callable[[str, dict], tuple[list[str], int, str, str]]


@dataclass(frozen=True)
class RegisteredTool:
    definition: ToolDefinitionView
    executor: ToolExecutor | None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        when_to_use: str = "",
        when_not_to_use: str = "",
        allowed_roles: list[AgentRole],
        mutability: Mutability,
        requires_user_approval: bool,
        idempotent: bool,
        timeout_seconds: int,
        executor: ToolExecutor | None = None,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        preconditions: list[str] | None = None,
        side_effect: str = "none",
        execution_kind: str = "engineering",
        category: str = "common",
        enabled: bool = True,
        always_available: bool = False,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            definition=ToolDefinitionView(
                name=name,
                description=description,
                when_to_use=when_to_use,
                when_not_to_use=when_not_to_use,
                allowed_roles=allowed_roles,
                mutability=mutability,
                requires_user_approval=requires_user_approval,
                idempotent=idempotent,
                timeout_seconds=timeout_seconds,
                input_schema=input_schema or {"type": "object", "properties": {}},
                output_schema=output_schema or {"type": "object"},
                preconditions=preconditions or [],
                side_effect=side_effect,
                execution_kind=execution_kind,
                category=category,
                enabled=enabled,
                always_available=always_available,
            ),
            executor=executor,
        )

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self) -> list[ToolDefinitionView]:
        return [tool.definition for tool in self._tools.values()]

    def for_role(self, role: AgentRole) -> list[ToolDefinitionView]:
        return [
            tool.definition
            for tool in self._tools.values()
            if tool.definition.enabled and role in tool.definition.allowed_roles
        ]

    def metadata_for_role(self, role: AgentRole) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "when_to_use": tool.when_to_use,
                "when_not_to_use": tool.when_not_to_use,
                "category": tool.category,
                "always_available": tool.always_available,
            }
            for tool in self.for_role(role)
        ]

    def set_enabled(self, name: str, enabled: bool) -> None:
        registered = self.get(name)
        self._tools[name] = RegisteredTool(
            definition=registered.definition.model_copy(update={"enabled": enabled}),
            executor=registered.executor,
        )


tool_registry = ToolRegistry()
