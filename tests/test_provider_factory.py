from app.config.model_settings import ModelEndpointSettings
from app.models.factory import ProviderFactory


class _SettingsService:
    def __init__(self) -> None:
        self.listener = None

    def register_change_listener(self, listener) -> None:
        self.listener = listener


def _endpoint(model: str = "model-a") -> ModelEndpointSettings:
    return ModelEndpointSettings(
        enabled=True,
        provider="openai",
        api_style="responses",
        model=model,
        base_url="https://api.example.test/v1",
        api_key="sk-test",
        timeout_seconds=60,
        max_output_tokens=1000,
        api_key_source="saved",
    )


def test_factory_reuses_equal_configuration_and_invalidates_on_change(monkeypatch) -> None:
    created = []
    clients = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.closed = False
            clients.append(self)

        def close(self) -> None:
            self.closed = True

    class FakeProvider:
        def __init__(self, settings, *, client) -> None:
            self.settings = settings
            self.client = client
            created.append(self)

    monkeypatch.setattr("app.models.factory.OpenAIModelProvider", FakeProvider)
    monkeypatch.setattr("app.models.factory.OpenAI", FakeClient)
    service = _SettingsService()
    factory = ProviderFactory(service)  # type: ignore[arg-type]

    first = factory.get(_endpoint())
    second = factory.get(_endpoint())
    other = factory.get(_endpoint("model-b"))

    assert first is second
    assert other is not first
    assert len(created) == 2
    assert factory.size == 2
    assert factory.client_size == 1
    assert first.client is other.client

    service.listener()
    assert clients[0].closed is True
    assert factory.size == 0
    assert factory.get(_endpoint()) is not first
