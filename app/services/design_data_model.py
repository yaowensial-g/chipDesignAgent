"""Structured design-data extraction through the authorized DeepSeek endpoint."""

import os
from collections.abc import Callable

from app.config.model_settings import model_settings_service
from app.models.factory import provider_factory


StructuredExtractor = Callable[[str, dict], object]
DEEPSEEK_DESIGN_DATA_URL = "https://api.deepseek.com"


def deepseek_design_model(operation: str, output_field: str) -> StructuredExtractor:
    def generate(prompt: str, payload: dict) -> object:
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        if mode in {"test", "mock", "deterministic"}:
            return {output_field: []}

        endpoint = model_settings_service.effective().main_endpoint()
        if endpoint.provider != "deepseek" or endpoint.base_url.rstrip("/") != DEEPSEEK_DESIGN_DATA_URL:
            raise RuntimeError("Design extraction is authorized only for https://api.deepseek.com")
        if not endpoint.ready:
            raise RuntimeError("The approved DeepSeek model is not enabled or configured")

        return provider_factory.get(endpoint, direct_only=True).generate_json(
            prompt, payload, operation=operation, expected_shape=f'{{"{output_field}":[]}}',
            repair_json=False,
        )

    return generate
