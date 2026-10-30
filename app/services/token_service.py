"""模型无关的 Token 预算估算。

项目必须支持 DeepSeek 等 OpenAI-compatible 模型，不能把上下文管理绑定到某一个
Tokenizer。若环境安装了 tiktoken 就使用它；否则采用偏保守的中英文估算。估算并不
替代模型厂商的精确计费，但远好于过去按字符数截断整篇文档的做法。
"""

from __future__ import annotations

import re


class TokenCounter:
    def __init__(self) -> None:
        self._encoder = None
        try:
            import tiktoken  # type: ignore

            self._encoder = tiktoken.get_encoding("cl100k_base")
        except (ImportError, ValueError):
            # tiktoken 是可选优化，不作为运行时硬依赖。
            self._encoder = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoder is not None:
            return len(self._encoder.encode(text))
        # 中文字符通常接近一个 token；英文按单词/数字/标点计数，并乘少量安全系数。
        chinese = len(re.findall(r"[\u3400-\u9fff]", text))
        non_chinese = re.sub(r"[\u3400-\u9fff]", " ", text)
        units = len(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s]", non_chinese))
        return max(1, chinese + int(units * 1.15))

    def trim(self, text: str, max_tokens: int) -> str:
        """保留文本开头到预算内；仅作为最终安全阀，RAG 排序发生在此之前。"""
        if max_tokens <= 0:
            return ""
        if self.count(text) <= max_tokens:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(text[:middle]) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + "\n[内容因本次 Token 预算截断]"


token_counter = TokenCounter()
