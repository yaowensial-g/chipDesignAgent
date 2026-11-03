"""模型配置的读取、合并和本地持久化。

配置优先级从高到低：环境变量 → 网页保存 JSON → 仓库默认 TOML。
API Key 单独经过 Windows DPAPI 保护，公开接口永远不返回明文。
"""

import json
import os
import threading
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.config.secret_protection import protect_secret, unprotect_secret


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "model.toml"
SAVED_CONFIG_PATH = PROJECT_ROOT / "data" / "model_settings.json"


@dataclass(frozen=True)
class ModelEndpointSettings:
    """单个模型端点的完整不可变配置，可直接交给 Provider Factory。"""

    enabled: bool
    provider: str
    api_style: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: int
    max_output_tokens: int
    api_key_source: str

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key.strip())


@dataclass(frozen=True)
class EffectiveModelSettings:
    """主模型与 Selector 完成三层合并后的兼容配置。

    前九个字段保持旧版构造契约。新增 Selector 字段都有默认值，因此已有测试、
    部署脚本和内部调用无需一次性迁移。
    """

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
    selector_inherit_main: bool = True
    selector_provider: str = ""
    selector_api_style: str = ""
    selector_base_url: str = ""
    selector_api_key: str = ""
    selector_timeout_seconds: int = 120
    selector_api_key_source: str = "none"

    @property
    def ready(self) -> bool:
        # enabled 表示用户主动启用，api_key 表示具备调用条件；两者缺一不可。
        return self.enabled and bool(self.api_key.strip())

    @property
    def selector_ready(self) -> bool:
        return self.selector_endpoint().ready

    def main_endpoint(self) -> ModelEndpointSettings:
        """返回主 Agent 使用的标准端点配置。"""
        return ModelEndpointSettings(
            enabled=self.enabled,
            provider=self.provider,
            api_style=self.api_style,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            api_key_source=self.api_key_source,
        )

    def selector_endpoint(self) -> ModelEndpointSettings:
        """按继承开关生成 Selector 的完整端点配置。"""
        if self.selector_inherit_main:
            return ModelEndpointSettings(
                enabled=self.enabled,
                provider=self.provider,
                api_style=self.api_style,
                model=self.selector_model,
                base_url=self.base_url,
                api_key=self.api_key,
                timeout_seconds=self.timeout_seconds,
                max_output_tokens=self.selector_max_output_tokens,
                api_key_source=self.api_key_source,
            )
        return ModelEndpointSettings(
            enabled=self.enabled,
            provider=self.selector_provider or self.provider,
            api_style=self.selector_api_style or self.api_style,
            model=self.selector_model,
            base_url=(self.selector_base_url or self.base_url).rstrip("/"),
            api_key=self.selector_api_key,
            timeout_seconds=self.selector_timeout_seconds,
            max_output_tokens=self.selector_max_output_tokens,
            api_key_source=self.selector_api_key_source,
        )


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
        self._change_listeners: list[Callable[[], None]] = []

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

        enabled = _as_bool(value("enabled", "CHIP_AGENT_MODEL_ENABLED"))
        api_style = str(value("api_style", "CHIP_AGENT_API_STYLE"))
        base_url = str(value("base_url", base_url_environment_name)).rstrip("/")
        timeout_seconds = int(value("timeout_seconds", "CHIP_AGENT_MODEL_TIMEOUT"))

        # 旧配置没有 inherit/provider/base_url/key 等 Selector 字段。默认继承主模型
        # 可完整复现旧行为；只有用户明确关闭继承时才读取独立端点。
        selector_inherit_main = _as_bool(
            value("selector_inherit_main", "CHIP_AGENT_SELECTOR_INHERIT_MAIN", True)
        )
        selector_provider = str(
            value("selector_provider", "CHIP_AGENT_SELECTOR_PROVIDER", provider)
        )
        selector_api_style = str(
            value("selector_api_style", "CHIP_AGENT_SELECTOR_API_STYLE", api_style)
        )
        selector_base_url = str(
            value(
                "selector_base_url",
                "CHIP_AGENT_SELECTOR_BASE_URL",
                _default_base_url(selector_provider),
            )
        ).rstrip("/")
        selector_timeout_seconds = int(
            value(
                "selector_timeout_seconds",
                "CHIP_AGENT_SELECTOR_TIMEOUT",
                timeout_seconds,
            )
        )
        selector_environment_key = (
            os.getenv("CHIP_AGENT_SELECTOR_API_KEY", "").strip()
            or os.getenv(
                "DEEPSEEK_SELECTOR_API_KEY"
                if selector_provider == "deepseek"
                else "OPENAI_SELECTOR_API_KEY",
                "",
            ).strip()
        )
        selector_saved_key = ""
        selector_key_field = f"selector_{selector_provider}_api_key_protected"
        if saved.get(selector_key_field):
            selector_saved_key = unprotect_secret(str(saved[selector_key_field])).strip()
        selector_api_key = selector_environment_key or selector_saved_key
        selector_key_source = (
            "environment"
            if selector_environment_key
            else ("saved" if selector_saved_key else "none")
        )

        return EffectiveModelSettings(
            enabled=enabled,
            provider=provider,
            api_style=api_style,
            model=str(value("model", "CHIP_AGENT_MODEL")),
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
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
            selector_inherit_main=selector_inherit_main,
            selector_provider=selector_provider,
            selector_api_style=selector_api_style,
            selector_base_url=selector_base_url,
            selector_api_key=selector_api_key,
            selector_timeout_seconds=selector_timeout_seconds,
            selector_api_key_source=selector_key_source,
        )

    def save(self, updates: dict[str, object]) -> EffectiveModelSettings:
        """保存网页覆盖配置，并使用临时文件替换避免写一半导致 JSON 损坏。"""
        with self._lock:
            current = self._load_saved()
            updates = dict(updates)
            clear_api_key = bool(updates.pop("clear_api_key", False))
            api_key = updates.pop("api_key", None)
            selector_clear_api_key = bool(updates.pop("selector_clear_api_key", False))
            selector_api_key = updates.pop("selector_api_key", None)
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

            selector_provider = str(current.get("selector_provider", provider))
            selector_key_field = f"selector_{selector_provider}_api_key_protected"
            if selector_clear_api_key:
                current.pop(selector_key_field, None)
            elif isinstance(selector_api_key, str) and selector_api_key.strip():
                current[selector_key_field] = protect_secret(selector_api_key.strip())

            self.saved_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.saved_path.with_name(f"model_settings.{uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(current, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.saved_path)
        settings = self.effective()
        self._notify_changed()
        return settings

    def public_view(self) -> dict[str, object]:
        """返回前端可见配置：删除明文 Key，只保留存在标记和掩码。"""
        settings = self.effective()
        view = asdict(settings)
        view.pop("api_key")
        view.pop("selector_api_key")
        view["has_api_key"] = bool(settings.api_key)
        view["api_key_masked"] = _mask_key(settings.api_key)
        view["ready"] = settings.ready
        selector_endpoint = settings.selector_endpoint()
        view["selector_has_api_key"] = bool(selector_endpoint.api_key)
        view["selector_api_key_masked"] = _mask_key(selector_endpoint.api_key)
        view["selector_api_key_source"] = selector_endpoint.api_key_source
        view["selector_ready"] = selector_endpoint.ready
        return view

    def register_change_listener(self, listener: Callable[[], None]) -> None:
        """注册配置变更监听器；主要用于让 Provider Factory 释放旧 Client。"""
        with self._lock:
            if listener not in self._change_listeners:
                self._change_listeners.append(listener)

    def _notify_changed(self) -> None:
        # 复制后再调用，避免 listener 回调中读取配置造成锁内重入链过长。
        with self._lock:
            listeners = tuple(self._change_listeners)
        for listener in listeners:
            listener()

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


def _default_base_url(provider: str) -> str:
    return "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"


model_settings_service = ModelSettingsService()

