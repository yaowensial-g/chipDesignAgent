import json
import re
from typing import Any

from openai import OpenAI

from app.config.model_settings import EffectiveModelSettings


class ModelInvocationError(RuntimeError):
    """Raised when the configured model cannot complete a request."""


class OpenAIModelProvider:
    def __init__(self, settings: EffectiveModelSettings) -> None:
        if not settings.api_key:
            raise ModelInvocationError("尚未配置 API Key")
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
        )

    def generate_text(self, instructions: str, input_text: str) -> str:
        try:
            if self.settings.api_style == "chat_completions":
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
        request = (
            "请只返回合法 JSON，不要使用 Markdown 代码块。输入如下：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        expected_type = dict if expected_shape.lstrip().startswith("{") else list if expected_shape.lstrip().startswith("[") else (dict, list)
        text = self.generate_text(instructions, request).strip()
        parsed = _parse_json_output(text)
        if parsed is not None and not isinstance(parsed, expected_type):
            parsed = None
        if parsed is None:
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
        """Ask the provider for exactly one next tool call.

        Both OpenAI Responses and DeepSeek-compatible Chat Completions are
        normalized to {name, arguments, reason}. A textual answer becomes the
        reply_to_user tool so the orchestration path remains uniform.
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
        result = self.generate_text(
            "你是连接检查器。严格按要求回答。",
            "只回答 CHIP_AGENT_CONNECTION_OK",
        )
        if "CHIP_AGENT_CONNECTION_OK" not in result:
            raise ModelInvocationError("模型已响应，但未返回预期的连接检查结果")
        return result.strip()


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    return message[:500] or exc.__class__.__name__


def _parse_tool_arguments(value: str | dict[str, Any] | None) -> dict[str, Any]:
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
    """Parse JSON from plain, fenced, or prose-wrapped model output."""

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

