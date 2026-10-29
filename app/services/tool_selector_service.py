"""两阶段 Tool 选择中的第一阶段。

主 Agent 不应该直接看到系统全部 Tool。这个服务先用确定性权限规则过滤，
再把很短的 Tool Metadata 交给轻量模型，让它选出一个“小而够用”的候选集。
之后主 Agent 才会看到这些候选 Tool 的完整 JSON Schema。

这里刻意把“权限判断”和“语义相关性判断”分开：
- 权限判断由 Python 代码完成，结果不可被模型提示词绕过；
- 语义相关性可交给模型，因为选错候选最多导致询问或回退，不会直接获得权限。
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.agents.runtime import runtime
from app.config.model_settings import model_settings_service
from app.domain.enums import AgentRole
from app.domain.models import ToolDefinitionView, ToolShortlist
from app.tools.registry import ToolRegistry, tool_registry


class ToolSelectorService:
    """按权限过滤后，使用轻量 Metadata 生成 Tool 候选列表。"""

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
        """返回主 Agent 可见的 Tool 定义和可观测选择轨迹。

        返回值是二元组：
        1. ``list[ToolDefinitionView]``：候选 Tool 的完整定义，交给主 Agent；
        2. ``trace``：模型名、耗时、候选数和是否回退，保存进 Run 便于排查。

        即使 Selector 返回未知名称或越权名称，也只会在 ``authorized_by_name``
        中查找；不在白名单里的名称会被直接丢弃。
        """
        # 第一道安全边界：先按主 Agent 的角色和 Tool enabled 状态硬过滤。
        # 这一步发生在任何模型调用之前。
        authorized = self.registry.for_role(primary_agent)
        authorized_by_name = {tool.name: tool for tool in authorized}

        # Selector 只读取简短 Metadata，不读取完整参数 Schema，从而减少 Token，
        # 也避免大量相似参数干扰“这个任务需要哪类工具”的第一阶段判断。
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
            # Selector 只是性能和准确率优化层，不应该成为单点故障。
            # 调用失败时回退到 Intent 候选和基础 Tool，让主链仍能安全响应用户。
            proposed = []
            reason = f"Selector 失败，使用安全回退：{exc.__class__.__name__}"
            fallback = True

        selected_names: list[str] = []
        for name in proposed if isinstance(proposed, list) else []:
            normalized = str(name)
            # 这里再次用授权字典校验模型输出。模型返回一个名称不代表它获得权限。
            if normalized in authorized_by_name and normalized not in selected_names:
                selected_names.append(normalized)

        # reply_to_user / ask_user 是结束或暂停 Run 所必需的“逃生 Tool”。
        # 即使 Selector 忘记返回，也要由后端加入候选集合。
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

        # 至少保留两个位置给 reply_to_user 和 ask_user。上限同时控制主 Agent
        # 接收的 Schema 数量，防止工具规模增长后上下文无限膨胀。
        limit = max(2, settings.selector_max_tools)
        selected_names = selected_names[:limit]
        shortlist = ToolShortlist(
            selected_tools=selected_names,
            reason=reason,
            used_fallback=fallback,
        )
        # trace 不含 API Key 或完整 Prompt，可以安全写入 Run 作为调试证据。
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
