import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config.model_settings import ModelSettingsService, model_settings_service
from app.config.model_settings import EffectiveModelSettings
from app.agents.runtime import runtime
from app.main import app


client = TestClient(app)


def _defaults(path: Path) -> None:
    path.write_text(
        """[model]
enabled = false
provider = "openai"
api_style = "responses"
model = "test-model"
base_url = "https://api.example.test/v1"
timeout_seconds = 60
max_output_tokens = 2048
""",
        encoding="utf-8",
    )


def test_saved_key_is_persistent_but_never_returned(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "model.toml"
    saved = tmp_path / "model_settings.json"
    _defaults(defaults)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CHIP_AGENT_MODEL_ENABLED", "true")
    service = ModelSettingsService(defaults, saved)

    service.save({"enabled": True, "api_key": "sk-secret-value-1234", "model": "real-model"})

    saved_payload = json.loads(saved.read_text(encoding="utf-8"))
    assert "sk-secret-value-1234" not in saved.read_text(encoding="utf-8")
    assert saved_payload["openai_api_key_protected"]
    view = service.public_view()
    assert view["ready"] is True
    assert view["has_api_key"] is True
    assert view["api_key_masked"].endswith("1234")
    assert "api_key" not in view


def test_empty_key_preserves_existing_key_and_clear_removes_it(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "model.toml"
    saved = tmp_path / "model_settings.json"
    _defaults(defaults)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = ModelSettingsService(defaults, saved)
    service.save({"api_key": "sk-keep-me"})

    service.save({"api_key": "", "model": "changed-model"})
    assert service.effective().api_key == "sk-keep-me"

    service.save({"clear_api_key": True})
    assert service.effective().api_key == ""


def test_model_settings_api_masks_secret(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "model.toml"
    saved = tmp_path / "model_settings.json"
    _defaults(defaults)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CHIP_AGENT_MODEL_ENABLED", "true")
    monkeypatch.setattr(model_settings_service, "default_path", defaults)
    monkeypatch.setattr(model_settings_service, "saved_path", saved)

    response = client.put(
        "/api/v1/settings/model",
        json={
            "enabled": True,
            "provider": "openai",
            "api_style": "responses",
            "model": "real-model",
            "base_url": "https://api.example.test/v1",
            "api_key": "sk-api-route-secret",
            "timeout_seconds": 90,
            "max_output_tokens": 4096,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["has_api_key"] is True
    assert "sk-api-route-secret" not in response.text
    assert "api_key" not in payload


def test_connection_endpoint_uses_saved_settings(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "model.toml"
    saved = tmp_path / "model_settings.json"
    _defaults(defaults)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(model_settings_service, "default_path", defaults)
    monkeypatch.setattr(model_settings_service, "saved_path", saved)
    model_settings_service.save({"api_key": "sk-test", "enabled": True})

    class FakeProvider:
        def __init__(self, settings):
            assert settings.model == "test-model"

        def test_connection(self):
            return "CHIP_AGENT_CONNECTION_OK"

    monkeypatch.setattr("app.api.routes.OpenAIModelProvider", FakeProvider)
    response = client.post("/api/v1/settings/model/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "模型连接成功",
        "model": "test-model",
    }


def test_configurable_runtime_switches_to_real_model_when_ready(monkeypatch) -> None:
    monkeypatch.setenv("CHIP_AGENT_MODEL_ENABLED", "true")
    settings = EffectiveModelSettings(
        enabled=True,
        provider="openai",
        api_style="responses",
        model="test-model",
        base_url="https://api.example.test/v1",
        api_key="sk-test",
        timeout_seconds=60,
        max_output_tokens=2048,
        api_key_source="saved",
    )

    class FakeLLMRuntime:
        def architecture_analysis(self, request):
            return {"summary": "来自真实模型路径", "sources": []}

    monkeypatch.setattr(model_settings_service, "effective", lambda: settings)
    monkeypatch.setattr(runtime, "llm", FakeLLMRuntime())

    result = runtime.architecture_analysis({"module_name": "demo"})
    assert result["summary"] == "来自真实模型路径"


def test_deepseek_uses_its_own_encrypted_key(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "model.toml"
    saved = tmp_path / "model_settings.json"
    _defaults(defaults)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CHIP_AGENT_MODEL_ENABLED", "true")
    service = ModelSettingsService(defaults, saved)

    service.save(
        {
            "enabled": True,
            "provider": "deepseek",
            "api_style": "responses",
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-deepseek-secret",
        }
    )

    settings = service.effective()
    stored = json.loads(saved.read_text(encoding="utf-8"))
    assert settings.provider == "deepseek"
    assert settings.api_key == "sk-deepseek-secret"
    assert settings.ready is True
    assert stored["deepseek_api_key_protected"]
    assert "sk-deepseek-secret" not in saved.read_text(encoding="utf-8")

