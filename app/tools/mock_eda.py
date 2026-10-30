"""不依赖真实 EDA 环境的占位执行函数。

当前开发环境不是最终芯片工具环境，因此编译、仿真、Lint、CDC、综合和 STA 暂时只
验证“主 Agent 能否正确选择并调用 Tool”。本模块绝不启动进程，也不伪造通过结果；
返回字符串始终带 ``MOCK`` 标识，后续接入真实 Adapter 时可以整体替换这里。
"""

from __future__ import annotations

import json
from typing import Any


MOCK_EDA_STAGES = {
    "compilation": "RTL 编译",
    "simulation": "仿真",
    "lint": "Lint",
    "cdc": "CDC 检查",
    "synthesis": "逻辑综合",
    "sta": "静态时序分析",
}


def run_mock_eda(
    stage: str,
    module_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """进入指定 EDA 占位阶段并直接返回可展示字符串。"""
    normalized = stage.strip().lower()
    if normalized not in MOCK_EDA_STAGES:
        raise ValueError(f"不支持的 Mock EDA 阶段：{stage}")
    options = arguments or {}
    options_text = json.dumps(options, ensure_ascii=False, sort_keys=True)
    return (
        f"[MOCK EDA] 已调用{MOCK_EDA_STAGES[normalized]}占位函数；"
        f"目标模块：{module_name}；参数：{options_text}。"
        "本次没有启动编译器、仿真器或其他外部进程，也没有产生真实工程结论；"
        "该结果仅用于验证 Multi-Agent 的 Tool 选择、Function Call 和 Run 状态链路。"
    )
