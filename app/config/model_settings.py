"""模型配置的读取、合并和本地持久化。

配置优先级从高到低：环境变量 → 网页保存 JSON → 仓库默认 TOML。
API Key 单独经过 Windows DPAPI 保护，公开接口永远不返回明文。
"""

import json
import os
import threading
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.config.secret_protection import protect_secret, unprotect_secret


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "model.toml"
SAVED_CONFIG_PATH = PROJECT_ROOT / "data" / "model_settings.json"


@dataclass(frozen=True)
class EffectiveModelSettings:
    """完成三层合并后，Runtime 实际使用的不可变配置。"""
    enabled: bool
    provider: str
    api_style: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: int
    max_output_tokens: int
    api_key_source: str
    selector_model: str = "deepseek-chat"
    selector_max_tools: int = 6
    selector_max_output_tokens: int = 1000

    @property
    def ready(self) -> bool:
        # enabled 表示用户主动启用，api_key 表示具备调用条件；两者缺一不可。
        return self.enabled and bool(self.api_key.strip())


class ModelSettingsService:
    """加载默认值并保存本地覆盖，同时避免暴露密钥。"""

    def __init__(
        self,
        default_path: Path = DEFAULT_CONFIG_PATH,
        saved_path: Path = SAVED_CONFIG_PATH,
    ) -> None:
        self.default_path = default_path
        self.saved_path = saved_path
        self._lock = threading.RLock()

    def effective(self) -> EffectiveModelSettings:
        """按优先级合并配置，并解密当前 Provider 对应的 Key。"""
        with self._lock:
            defaults = self._load_defaults()
            saved = self._load_saved()

        provider = str(value_from_sources(saved, defaults, "provider", "CHIP_AGENT_MODEL_PROVIDER"))
        key_environment_name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
        base_url_environment_name = "DEEPSEEK_BASE_URL" if provider == "deepseek" else "OPENAI_BASE_URL"
        # 环境变量最适合 CI/部署，因此优先于网页保存的本机密文。
        environment_key = os.getenv(key_environment_name, "").strip()
        saved_key = ""
        provider_key_field = f"{provider}_api_key_protected"
        if saved.get(provider_key_field):
            saved_key = unprotect_secret(str(saved[provider_key_field])).strip()
        elif provider == "openai" and saved.get("api_key_protected"):
            saved_key = unprotect_secret(str(saved["api_key_protected"])).strip()
        elif provider == "openai" and saved.get("api_key"):
            # One-time compatibility for an earlier local development format.
            saved_key = str(saved["api_key"]).strip()
        api_key = environment_key or saved_key
        key_source = "environment" if environment_key else ("saved" if saved_key else "none")

        def value(
            name: str,
            environment_name: str | None = None,
            fallback: object | None = None,
        ):
            return value_from_sources(saved, defaults, name, environment_name, fallback)

        return EffectiveModelSettings(
            enabled=_as_bool(value("enabled", "CHIP_AGENT_MODEL_ENABLED")),
            provider=provider,
            api_style=str(value("api_style", "CHIP_AGENT_API_STYLE")),
            model=str(value("model", "CHIP_AGENT_MODEL")),
            base_url=str(value("base_url", base_url_environment_name)).rstrip("/"),
            api_key=api_key,
            timeout_seconds=int(value("timeout_seconds", "CHIP_AGENT_MODEL_TIMEOUT")),
            max_output_tokens=int(value("max_output_tokens", "CHIP_AGENT_MAX_OUTPUT_TOKENS")),
            selector_model=str(
                value("selector_model", "CHIP_AGENT_SELECTOR_MODEL", "deepseek-chat")
            ),
            selector_max_tools=int(
                value("selector_max_tools", "CHIP_AGENT_SELECTOR_MAX_TOOLS", 6)
            ),
            selector_max_output_tokens=int(
                value(
                    "selector_max_output_tokens",
                    "CHIP_AGENT_SELECTOR_MAX_OUTPUT_TOKENS",
                    1000,
                )
            ),
            api_key_source=key_source,
        )

    def save(self, updates: dict[str, object]) -> EffectiveModelSettings:
        """保存网页覆盖配置，并使用临时文件替换避免写一半导致 JSON 损坏。"""
        with self._lock:
            current = self._load_saved()
            updates = dict(updates)
            clear_api_key = bool(updates.pop("clear_api_key", False))
            api_key = updates.pop("api_key", None)
            current.update({key: value for key, value in updates.items() if value is not None})
            provider = str(current.get("provider", self._load_defaults()["provider"]))
            provider_key_field = f"{provider}_api_key_protected"
            if clear_api_key:
                current.pop(provider_key_field, None)
                if provider == "openai":
                    current.pop("api_key", None)
                    current.pop("api_key_protected", None)
            elif isinstance(api_key, str) and api_key.strip():
                # 不把明文 API Key 写入 JSON；每个 Provider 使用独立密文字段，
                # 避免切换 OpenAI/DeepSeek 时误用另一家的凭据。
                current[provider_key_field] = protect_secret(api_key.strip())
                if provider == "openai":
                    current.pop("api_key", None)
                    current.pop("api_key_protected", None)

            self.saved_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.saved_path.with_name(f"model_settings.{uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(current, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.saved_path)
        return self.effective()

    def public_view(self) -> dict[str, object]:
        """返回前端可见配置：删除明文 Key，只保留存在标记和掩码。"""
        settings = self.effective()
        view = asdict(settings)
        view.pop("api_key")
        view["has_api_key"] = bool(settings.api_key)
        view["api_key_masked"] = _mask_key(settings.api_key)
        view["ready"] = settings.ready
        return view

    def _load_defaults(self) -> dict[str, object]:
        with self.default_path.open("rb") as stream:
            payload = tomllib.load(stream)
        return payload["model"]

    def _load_saved(self) -> dict[str, object]:
        if not self.saved_path.exists():
            return {}
        try:
            payload = json.loads(self.saved_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Saved model settings are not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Saved model settings must be a JSON object")
        return payload


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def value_from_sources(
    saved: dict[str, object],
    defaults: dict[str, object],
    name: str,
    environment_name: str | None = None,
    fallback: object | None = None,
):
    """按环境变量、保存值、默认值、兼容 fallback 的顺序取配置。"""
    if environment_name and os.getenv(environment_name) is not None:
        return os.environ[environment_name]
    if name in saved:
        return saved[name]
    if name in defaults:
        return defaults[name]
    if fallback is not None:
        return fallback
    raise KeyError(name)


def _mask_key(api_key: str) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "•" * len(api_key)
    return f"{api_key[:3]}{'•' * 8}{api_key[-4:]}"


model_settings_service = ModelSettingsService()

