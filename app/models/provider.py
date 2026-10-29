"""外部模型协议适配层。

上层 Runtime 不直接依赖某一家 SDK 的响应对象。本模块把 OpenAI Responses API 和
OpenAI-compatible Chat Completions 统一成文本、JSON 或 ``ToolSelection`` 三种结果。
这里只做协议适配和格式校验，不决定 Tool 权限，也不执行任何工程动作。
"""

import json
import re
from typing import Any

from openai import OpenAI

from app.config.model_settings import EffectiveModelSettings


class ModelInvocationError(RuntimeError):
    """模型连接、响应格式或 Tool 参数不满足约定时抛出的统一异常。"""


class OpenAIModelProvider:
    """把不同 API 风格归一化成项目内部稳定接口。"""
    def __init__(self, settings: EffectiveModelSettings) -> None:
        # Provider 不允许在没有 Key 时初始化，避免请求发出后才得到难理解的 401。
        if not settings.api_key:
            raise ModelInvocationError("尚未配置 API Key")
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
        )

    def generate_text(self, instructions: str, input_text: str) -> str:
        """生成自由文本，并处理不同供应商的 role 和 Token 参数差异。"""
        try:
            if self.settings.api_style == "chat_completions":
                # DeepSeek 兼容接口使用 system；当前 OpenAI 路径使用 developer。
                messages = [
                    {
                        "role": "system" if self.settings.provider == "deepseek" else "developer",
                        "content": instructions,
                    },
                    {"role": "user", "content": input_text},
                ]
                options = {
                    "model": self.settings.model,
                    "messages": messages,
                }
                if self.settings.provider == "deepseek":
                    options["max_tokens"] = self.settings.max_output_tokens
                else:
                    options["max_completion_tokens"] = self.settings.max_output_tokens
                completion = self.client.chat.completions.create(**options)
                return completion.choices[0].message.content or ""

            # Responses API 将 instructions 与 input 分开，并可显式禁止服务端存储。
            options = dict(
                model=self.settings.model,
                instructions=instructions,
                input=input_text,
                max_output_tokens=self.settings.max_output_tokens,
            )
            if self.settings.provider == "openai":
                options["store"] = False
            response = self.client.responses.create(**options)
            return response.output_text
        except Exception as exc:
            raise ModelInvocationError(f"模型调用失败：{_safe_error(exc)}") from exc

    def generate_json(
        self,
        instructions: str,
        payload: dict[str, Any],
        *,
        operation: str = "结构化 Agent",
        expected_shape: str = "JSON 对象或数组",
    ) -> dict[str, Any] | list[Any]:
        """请求结构化 JSON，并在首次解析失败后只做一次格式修复。

        修复 Prompt 明确禁止改变业务结论。只重试一次是为了避免模型无限自修复，
        同时让真正的格式问题以 ``ModelInvocationError`` 暴露给用户。
        """
        request = (
            "请只返回合法 JSON，不要使用 Markdown 代码块。输入如下：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        expected_type = dict if expected_shape.lstrip().startswith("{") else list if expected_shape.lstrip().startswith("[") else (dict, list)
        text = self.generate_text(instructions, request).strip()
        # 先兼容纯 JSON、Markdown Fence 和被解释文字包围的 JSON。
        parsed = _parse_json_output(text)
        if parsed is not None and not isinstance(parsed, expected_type):
            parsed = None
        if parsed is None:
            # 第二次调用只负责修复括号、引号等格式，不允许补充或重写结论。
            repair_text = self.generate_text(
                "你是 JSON 格式修复器。只修复格式，不新增、删除或改变原始结论。只返回合法 JSON，不要 Markdown 代码块或解释。",
                f"目标结构：{expected_shape}\n请修复以下模型输出：\n{text[:20_000]}",
            ).strip()
            parsed = _parse_json_output(repair_text)
            if parsed is not None and not isinstance(parsed, expected_type):
                parsed = None
        if parsed is None:
            raise ModelInvocationError(
                f"{operation}没有返回有效 JSON；自动提取和一次格式修复后仍无法解析"
            )
        if not isinstance(parsed, (dict, list)):
            raise ModelInvocationError(f"{operation}的 JSON 输出类型不符合约定")
        return parsed

    def choose_tool(
        self,
        instructions: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """要求 Provider 选择一个下一步 Tool Call。

        两种 API 都归一化为 ``{name, arguments, reason}``。如果模型没有调用函数而是
        直接返回文本，则转换成 ``reply_to_user``，从而保持聊天与工程任务同一控制链。
        """

        input_text = json.dumps(payload, ensure_ascii=False)
        try:
            if self.settings.api_style == "chat_completions":
                messages = [
                    {
                        "role": "system" if self.settings.provider == "deepseek" else "developer",
                        "content": instructions,
                    },
                    {"role": "user", "content": input_text},
                ]
                options: dict[str, Any] = {
                    "model": self.settings.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }
                if self.settings.provider == "deepseek":
                    options["max_tokens"] = self.settings.max_output_tokens
                else:
                    options["max_completion_tokens"] = self.settings.max_output_tokens
                completion = self.client.chat.completions.create(**options)
                message = completion.choices[0].message
                if message.tool_calls:
                    # 当前 Planner 一轮只执行一个 Tool，因此只读取第一个 Tool Call。
                    call = message.tool_calls[0]
                    return {
                        "name": call.function.name,
                        "arguments": _parse_tool_arguments(call.function.arguments),
                        "reason": "模型 Function Calling",
                    }
                return {
                    "name": "reply_to_user",
                    "arguments": {"message": message.content or ""},
                    "reason": "模型直接回答已归一化为 Chat Tool",
                }

            # Responses API 的 Function Tool 格式与 Chat Completions 略有差异，
            # 在这里转换，避免 Runtime 同时了解两套 SDK 结构。
            response_tools = [
                {
                    "type": "function",
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "parameters": tool["function"]["parameters"],
                }
                for tool in tools
            ]
            options = {
                "model": self.settings.model,
                "instructions": instructions,
                "input": input_text,
                "tools": response_tools,
                "tool_choice": "auto",
                "max_output_tokens": self.settings.max_output_tokens,
            }
            if self.settings.provider == "openai":
                options["store"] = False
            response = self.client.responses.create(**options)
            for item in response.output:
                if getattr(item, "type", "") == "function_call":
                    return {
                        "name": item.name,
                        "arguments": _parse_tool_arguments(item.arguments),
                        "reason": "模型 Function Calling",
                    }
            return {
                "name": "reply_to_user",
                "arguments": {"message": response.output_text or ""},
                "reason": "模型直接回答已归一化为 Chat Tool",
            }
        except Exception as exc:
            raise ModelInvocationError(f"模型 Tool Calling 失败：{_safe_error(exc)}") from exc

    def test_connection(self) -> str:
        """执行最小连接探针；成功只说明端点/Key/模型可响应。"""
        result = self.generate_text(
            "你是连接检查器。严格按要求回答。",
            "只回答 CHIP_AGENT_CONNECTION_OK",
        )
        if "CHIP_AGENT_CONNECTION_OK" not in result:
            raise ModelInvocationError("模型已响应，但未返回预期的连接检查结果")
        return result.strip()


def _safe_error(exc: Exception) -> str:
    """压缩外部异常，避免把多行响应或过长 HTML 原样返回前端。"""
    message = str(exc).replace("\n", " ").strip()
    return message[:500] or exc.__class__.__name__


def _parse_tool_arguments(value: str | dict[str, Any] | None) -> dict[str, Any]:
    """把 SDK 可能返回的字符串参数解析成字典，并拒绝数组等错误类型。"""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelInvocationError("模型返回了无法解析的 Tool 参数") from exc
    if not isinstance(parsed, dict):
        raise ModelInvocationError("模型 Tool 参数必须是 JSON 对象")
    return parsed


def _parse_json_output(text: str) -> dict[str, Any] | list[Any] | None:
    """从纯文本、代码块或解释文字包围的输出中提取 JSON。"""

    cleaned = text.strip().lstrip("\ufeff")
    candidates = [cleaned]
    candidates.extend(
        match.strip()
        for match in re.findall(r"```(?:json)?\s*([\s\S]*?)```", cleaned, flags=re.IGNORECASE)
    )
    candidates.extend(_balanced_json_candidates(cleaned))

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _balanced_json_candidates(text: str) -> list[str]:
    """扫描括号平衡片段，同时正确忽略字符串内部的括号和转义引号。"""
    candidates: list[str] = []
    for start, character in enumerate(text):
        if character not in "{[":
            continue
        stack: list[str] = []
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            current = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current in "{[":
                stack.append(current)
            elif current in "}]":
                if not stack:
                    break
                opening = stack.pop()
                if (opening, current) not in {("{", "}"), ("[", "]")}:
                    break
                if not stack:
                    candidates.append(text[start : position + 1])
                    break
    return candidates

