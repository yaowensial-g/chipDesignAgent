import pytest
from types import SimpleNamespace

from app.models.provider import ModelInvocationError, OpenAIModelProvider, _parse_json_output


def test_json_parser_accepts_fenced_and_prose_wrapped_output() -> None:
    assert _parse_json_output('```json\n{"issues": []}\n```') == {"issues": []}
    assert _parse_json_output('分析完成。结果如下：\n{"findings": []}\n请查收。') == {
        "findings": []
    }


def test_generate_json_repairs_malformed_output_once(monkeypatch) -> None:
    provider = object.__new__(OpenAIModelProvider)
    responses = iter([
        "这里是结果：{'issues': []}",
        '{"issues": []}',
    ])
    calls: list[str] = []

    def fake_generate_text(instructions: str, input_text: str) -> str:
        calls.append(instructions)
        return next(responses)

    monkeypatch.setattr(provider, "generate_text", fake_generate_text)
    result = provider.generate_json(
        "输出问题列表",
        {"documents": []},
        operation="规格质疑 Agent",
        expected_shape='{"issues":[]}',
    )

    assert result == {"issues": []}
    assert len(calls) == 2
    assert "JSON 格式修复器" in calls[1]


def test_generate_json_error_identifies_agent_after_failed_repair(monkeypatch) -> None:
    provider = object.__new__(OpenAIModelProvider)
    monkeypatch.setattr(provider, "generate_text", lambda *_: "无法提供结构化结果")

    with pytest.raises(ModelInvocationError, match="接口一致性评审 Agent"):
        provider.generate_json(
            "输出评审",
            {},
            operation="接口一致性评审 Agent（interface_reviewer）",
        )


def test_chat_completions_native_tool_call_is_normalized() -> None:
    provider = object.__new__(OpenAIModelProvider)
    provider.settings = SimpleNamespace(
        api_style="chat_completions",
        provider="deepseek",
        model="deepseek-test",
        max_output_tokens=1024,
    )
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="reply_to_user", arguments='{"message":"ok"}')
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    completion = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: completion))
    )

    result = provider.choose_tool(
        "choose",
        {"message": "hello"},
        [{"type": "function", "function": {"name": "reply_to_user", "description": "reply", "parameters": {"type": "object"}}}],
    )

    assert result["name"] == "reply_to_user"
    assert result["arguments"] == {"message": "ok"}


def test_responses_native_tool_call_is_normalized() -> None:
    provider = object.__new__(OpenAIModelProvider)
    provider.settings = SimpleNamespace(
        api_style="responses",
        provider="openai",
        model="gpt-test",
        max_output_tokens=1024,
    )
    response = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", name="ask_user", arguments='{"question":"which?"}')],
        output_text="",
    )
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: response)
    )

    result = provider.choose_tool(
        "choose",
        {"message": "check it"},
        [{"type": "function", "function": {"name": "ask_user", "description": "ask", "parameters": {"type": "object"}}}],
    )

    assert result["name"] == "ask_user"
    assert result["arguments"] == {"question": "which?"}
