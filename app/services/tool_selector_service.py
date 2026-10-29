from __future__ import annotations

from time import perf_counter
from typing import Any

from app.agents.runtime import runtime
from app.config.model_settings import model_settings_service
from app.domain.enums import AgentRole
from app.domain.models import ToolDefinitionView, ToolShortlist
from app.tools.registry import ToolRegistry, tool_registry


class ToolSelectorService:
    """Permission-first, metadata-only shortlist selection for the main Agent."""

    def __init__(self, registry: ToolRegistry = tool_registry) -> None:
        self.registry = registry

    def select(
        self,
        *,
        primary_agent: AgentRole,
        request: dict[str, Any],
        intent: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> tuple[list[ToolDefinitionView], dict[str, Any]]:
        authorized = self.registry.for_role(primary_agent)
        authorized_by_name = {tool.name: tool for tool in authorized}
        metadata = self.registry.metadata_for_role(primary_agent)
        settings = model_settings_service.effective()
        started = perf_counter()
        fallback = False

        try:
            raw = runtime.select_tool_names(
                request,
                intent,
                metadata,
                observations,
                primary_agent.value,
                settings.selector_max_tools,
            )
            proposed = raw.get("selected_tools", [])
            reason = str(raw.get("reason", ""))
        except Exception as exc:
            proposed = []
            reason = f"Selector 失败，使用安全回退：{exc.__class__.__name__}"
            fallback = True

        selected_names: list[str] = []
        for name in proposed if isinstance(proposed, list) else []:
            normalized = str(name)
            if normalized in authorized_by_name and normalized not in selected_names:
                selected_names.append(normalized)

        always = [tool.name for tool in authorized if tool.always_available]
        selected_names = always + [name for name in selected_names if name not in always]

        if not selected_names:
            fallback = True
            candidates = [
                str(name)
                for name in intent.get("candidate_tools", [])
                if str(name) in authorized_by_name
            ]
            selected_names.extend(candidates[:1])
            for name in ("reply_to_user", "ask_user"):
                if name in authorized_by_name and name not in selected_names:
                    selected_names.append(name)
            reason = reason or "Selector 未返回有效 Tool，使用安全基础能力"

        limit = max(2, settings.selector_max_tools)
        selected_names = selected_names[:limit]
        shortlist = ToolShortlist(
            selected_tools=selected_names,
            reason=reason,
            used_fallback=fallback,
        )
        trace = {
            **shortlist.model_dump(mode="json"),
            "primary_agent": primary_agent.value,
            "authorized_count": len(authorized),
            "metadata_count": len(metadata),
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            "selector_model": settings.selector_model if settings.ready else "deterministic",
        }
        return [authorized_by_name[name] for name in selected_names], trace


tool_selector_service = ToolSelectorService()
