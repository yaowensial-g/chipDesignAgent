"""Tool 注册中心。

Registry 是模型与真实执行能力之间的安全边界。模型看到的是 Tool Definition，
真正可执行的 Python 函数保存在 ``RegisteredTool.executor`` 中。模型输出一个
同名字符串不会自动得到执行权限，调用方仍需从 Registry 查询并校验角色。
"""

from dataclasses import dataclass
from typing import Callable

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ToolDefinitionView


ToolExecutor = Callable[[str, dict], tuple[list[str], int, str, str]]


@dataclass(frozen=True)
class RegisteredTool:
    """将公开定义与后端执行器绑定；某些编排 Tool 的 executor 可以为空。"""
    definition: ToolDefinitionView
    executor: ToolExecutor | None


class ToolRegistry:
    """进程内 Tool 目录，负责注册、查询、角色过滤和启停。"""
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
        """注册一个 Tool，并把零散配置组装成强类型定义。

        ``allowed_roles`` 是后端强制权限，不是给模型看的建议；
        ``mutability`` 描述动作会不会修改状态；
        ``requires_user_approval`` 表示执行前是否还需要显式用户授权；
        ``always_available`` 只应用于 reply/ask 等结束与暂停能力。
        """
        # 重名通常表示模块导入或初始化顺序错误。直接失败比静默覆盖执行器安全。
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
        """按名称读取 Tool；未知名称转换成更容易排查的异常信息。"""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self) -> list[ToolDefinitionView]:
        """返回完整目录，主要用于诊断页面、文档和管理接口。"""
        return [tool.definition for tool in self._tools.values()]

    def for_role(self, role: AgentRole) -> list[ToolDefinitionView]:
        """返回某角色已授权且当前启用的 Tool。

        这是模型调用之前的权限过滤入口。禁用或越权 Tool 不会进入 Selector。
        """
        return [
            tool.definition
            for tool in self._tools.values()
            if tool.definition.enabled and role in tool.definition.allowed_roles
        ]

    def metadata_for_role(self, role: AgentRole) -> list[dict]:
        """生成轻量 Selector 使用的 Metadata，不包含完整参数 Schema。

        Selector 只需要判断“任务是否相关”，不负责填写参数。将完整 Schema 留给
        第二阶段主 Agent，可以减少 Token，也让两个模型的职责更清楚。
        """
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
        """启停 Tool，同时保留原执行器和其余不可变元数据。"""
        registered = self.get(name)
        self._tools[name] = RegisteredTool(
            definition=registered.definition.model_copy(update={"enabled": enabled}),
            executor=registered.executor,
        )


tool_registry = ToolRegistry()
