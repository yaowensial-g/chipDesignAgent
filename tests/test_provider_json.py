import pytest

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
