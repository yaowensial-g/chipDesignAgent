"""模型真实 usage 归一化与请求级聚合测试。"""

from types import SimpleNamespace

from app.config.model_settings import EffectiveModelSettings
from app.models.provider import OpenAIModelProvider
from app.services.token_usage_service import token_usage_tracker


def _settings(api_style: str) -> EffectiveModelSettings:
    return EffectiveModelSettings(
        enabled=True,
        provider="deepseek",
        api_style=api_style,
        model="test-model",
        base_url="https://example.invalid",
        api_key="test-key",
        timeout_seconds=30,
        max_output_tokens=100,
        api_key_source="test",
    )


def test_chat_completion_usage_is_recorded() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17),
    )
    provider = OpenAIModelProvider.__new__(OpenAIModelProvider)
    provider.settings = _settings("chat_completions")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )

    token = token_usage_tracker.start()
    assert provider.generate_text("instruction", "input") == "ok"
    usage = token_usage_tracker.finish(token)

    assert usage.as_dict() == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "model_calls": 1,
    }


def test_responses_usage_is_aggregated_across_calls() -> None:
    response = SimpleNamespace(
        output_text="ok",
        usage=SimpleNamespace(input_tokens=20, output_tokens=8, total_tokens=28),
    )
    provider = OpenAIModelProvider.__new__(OpenAIModelProvider)
    provider.settings = _settings("responses")
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: response)
    )

    token = token_usage_tracker.start()
    provider.generate_text("instruction", "first")
    provider.generate_text("instruction", "second")
    usage = token_usage_tracker.finish(token)

    assert usage.input_tokens == 40
    assert usage.output_tokens == 16
    assert usage.total_tokens == 56
    assert usage.model_calls == 2
