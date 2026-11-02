"""按一次用户请求聚合模型返回的真实 Token 用量。

一次聊天或工程任务可能在后端触发多次模型调用，例如 Intent 补全、轻量 Tool
Selector、主 Agent Function Calling、专家 Delegate 以及 JSON 格式修复。如果只记录
最后一次 SDK 响应，前端展示的成本会明显偏小。

本模块使用 ``ContextVar`` 保存当前请求的累计器。FastAPI 的并发请求各自拥有独立
上下文，不会把不同用户或不同会话的用量混在一起。Provider 只负责在每次拿到 SDK
响应后调用 ``record_response``；Orchestrator 负责在请求开始/结束时创建和读取累计器。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class TokenUsage:
    """项目内部统一的 Token 用量结构。

    Chat Completions 使用 prompt/completion 命名，Responses API 使用 input/output
    命名；进入项目后统一成 input/output，避免业务层了解两套 SDK 字段。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class TokenUsageTracker:
    """维护当前请求的 Token 累计值。"""

    def __init__(self) -> None:
        self._current: ContextVar[TokenUsage | None] = ContextVar(
            "chipflow_token_usage", default=None
        )

    def start(self) -> Token:
        """开始一个新的统计作用域，并返回用于恢复旧上下文的令牌。"""
        return self._current.set(TokenUsage())

    def finish(self, token: Token) -> TokenUsage:
        """取得最终快照并恢复进入作用域前的 ContextVar。"""
        usage = self.snapshot()
        self._current.reset(token)
        return usage

    def snapshot(self) -> TokenUsage:
        current = self._current.get()
        if current is None:
            return TokenUsage()
        return TokenUsage(**current.as_dict())

    def record_response(self, response: Any) -> None:
        """读取 OpenAI/DeepSeek SDK 响应中的 usage，并累加到当前请求。

        某些兼容端点或测试替身可能不返回 usage。此时仍记录一次模型调用，但不会用
        本地字符估算冒充厂商计费数据，因此 Token 数保持为 0。
        """
        current = self._current.get()
        if current is None:
            return

        usage = getattr(response, "usage", None)
        current.model_calls += 1
        if usage is None:
            return

        input_tokens = _integer_attribute(usage, "input_tokens", "prompt_tokens")
        output_tokens = _integer_attribute(usage, "output_tokens", "completion_tokens")
        total_tokens = _integer_attribute(usage, "total_tokens")
        # 少数 OpenAI-compatible 服务不返回 total_tokens，可以可靠地由两部分相加。
        if total_tokens <= 0 and (input_tokens > 0 or output_tokens > 0):
            total_tokens = input_tokens + output_tokens

        current.input_tokens += input_tokens
        current.output_tokens += output_tokens
        current.total_tokens += total_tokens


def _integer_attribute(value: Any, *names: str) -> int:
    """同时兼容 SDK 对象和测试中使用的字典响应。"""
    for name in names:
        raw = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                return 0
    return 0


token_usage_tracker = TokenUsageTracker()
