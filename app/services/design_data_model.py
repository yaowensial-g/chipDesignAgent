"""Structured design-data extraction through the authorized DeepSeek endpoint."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.context import ContextBuildRequest, ContextPurpose
from app.services.llm_invocation_service import LLMInvocationService

if TYPE_CHECKING:
    from app.services.context_builder_service import ContextBuilderService


StructuredExtractor = Callable[[str, dict], object]


@dataclass(frozen=True)
class DeepSeekDesignModel:
    operation: str
    output_field: str

    def generate(
        self, prompt: str, request: ContextBuildRequest, contexts: ContextBuilderService | None = None,
    ) -> object:
        if request.purpose not in {ContextPurpose.CONFIG_EXTRACTION, ContextPurpose.MEMORY_EXTRACTION}:
            raise ValueError("Design-data extraction requires an extraction context")
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        if mode in {"test", "mock", "deterministic"}:
            return {self.output_field: []}

        return LLMInvocationService(contexts).generate_json(
            request, prompt, operation=self.operation, expected_shape=f'{{"{self.output_field}":[]}}',
            repair_json=False,
        )


def deepseek_design_model(operation: str, output_field: str) -> DeepSeekDesignModel:
    return DeepSeekDesignModel(operation, output_field)


def invoke_extractor(
    model: DeepSeekDesignModel | StructuredExtractor, prompt: str, request: ContextBuildRequest,
    contexts: ContextBuilderService,
) -> object:
    if isinstance(model, DeepSeekDesignModel):
        return model.generate(prompt, request, contexts)
    # In-process extraction callbacks have the same profile without an external model call.
    return model(prompt, contexts.build_invocation(request, instructions=prompt).payload)
