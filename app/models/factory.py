"""线程安全的模型 Provider 生命周期管理。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from openai import OpenAI

from app.config.model_settings import (
    ModelEndpointSettings,
    ModelSettingsService,
    model_settings_service,
)
from app.models.provider import ModelInvocationError, OpenAIModelProvider


@dataclass(frozen=True)
class _ProviderKey:
    """完整端点配置键；密钥只参与内存比较，不会被记录或对外暴露。"""

    provider: str
    api_style: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: int
    max_output_tokens: int

    @classmethod
    def from_settings(cls, settings: ModelEndpointSettings) -> "_ProviderKey":
        return cls(
            provider=settings.provider,
            api_style=settings.api_style,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )


@dataclass(frozen=True)
class _ClientKey:
    """只包含 OpenAI Client 连接级配置，不含模型和输出 Token。"""

    base_url: str
    api_key: str
    timeout_seconds: int

    @classmethod
    def from_settings(cls, settings: ModelEndpointSettings) -> "_ClientKey":
        return cls(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
        )


class ProviderFactory:
    """按不可变配置复用 Provider，并在配置变化时原子清空缓存。"""

    def __init__(self, settings_service: ModelSettingsService = model_settings_service) -> None:
        self._lock = threading.RLock()
        self._providers: dict[_ProviderKey, OpenAIModelProvider] = {}
        self._clients: dict[_ClientKey, OpenAI] = {}
        settings_service.register_change_listener(self.clear)

    def get(self, settings: ModelEndpointSettings) -> OpenAIModelProvider:
        if not settings.api_key:
            raise ModelInvocationError("尚未配置 API Key")
        key = _ProviderKey.from_settings(settings)
        with self._lock:
            provider = self._providers.get(key)
            if provider is None:
                client_key = _ClientKey.from_settings(settings)
                client = self._clients.get(client_key)
                if client is None:
                    client = OpenAI(
                        api_key=settings.api_key,
                        base_url=settings.base_url,
                        timeout=settings.timeout_seconds,
                    )
                    self._clients[client_key] = client
                provider = OpenAIModelProvider(settings, client=client)
                self._providers[key] = provider
            return provider

    def clear(self) -> None:
        """先交换缓存再释放旧 Client，避免并发读取到半清理状态。"""
        with self._lock:
            clients = tuple(self._clients.values())
            self._providers = {}
            self._clients = {}
        for client in clients:
            try:
                client.close()
            except Exception:
                # Client 关闭失败不应阻止新配置生效。
                pass

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._providers)

    @property
    def client_size(self) -> int:
        with self._lock:
            return len(self._clients)


provider_factory = ProviderFactory()
