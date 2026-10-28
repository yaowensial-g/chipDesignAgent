from dataclasses import dataclass
from typing import Callable

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ToolDefinitionView


ToolExecutor = Callable[[str, dict], tuple[list[str], int, str, str]]


@dataclass(frozen=True)
class RegisteredTool:
    definition: ToolDefinitionView
    executor: ToolExecutor


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        allowed_roles: list[AgentRole],
        mutability: Mutability,
        requires_user_approval: bool,
        idempotent: bool,
        timeout_seconds: int,
        executor: ToolExecutor,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            definition=ToolDefinitionView(
                name=name,
                description=description,
                allowed_roles=allowed_roles,
                mutability=mutability,
                requires_user_approval=requires_user_approval,
                idempotent=idempotent,
                timeout_seconds=timeout_seconds,
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


tool_registry = ToolRegistry()
