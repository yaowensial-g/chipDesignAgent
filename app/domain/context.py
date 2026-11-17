"""Scope, purpose, and audit information for one model input."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.domain.orchestration import EvidenceRef


class ContextPurpose(StrEnum):
    CHAT_BASE = "CHAT_BASE"
    CHAT_RESPONSE = "CHAT_RESPONSE"
    CONFIG_EXTRACTION = "CONFIG_EXTRACTION"
    INTENT_ENRICHMENT = "INTENT_ENRICHMENT"
    TOOL_SELECTOR = "TOOL_SELECTOR"
    TOOL_CALL = "TOOL_CALL"
    GLOBAL_PLANNING = "GLOBAL_PLANNING"
    REFLECTION = "REFLECTION"
    REPLANNING = "REPLANNING"
    SPECIALIST_ANALYSIS = "SPECIALIST_ANALYSIS"
    DS_GENERATION = "DS_GENERATION"
    RTL_GENERATION = "RTL_GENERATION"
    VERIFICATION = "VERIFICATION"
    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    MEMORY_EXTRACTION = "MEMORY_EXTRACTION"
    CONVERSATION_SUMMARY = "CONVERSATION_SUMMARY"
    SUMMARY_COMPRESSION = "SUMMARY_COMPRESSION"
    JSON_REPAIR = "JSON_REPAIR"
    MODEL_HEALTH_CHECK = "MODEL_HEALTH_CHECK"


@dataclass(frozen=True)
class ContextBuildRequest:
    purpose: ContextPurpose
    project_id: str | None = None
    module_name: str | None = None
    session_id: str | None = None
    agent_role: str | None = None
    source_message_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    generation: int | None = None
    query: str = ""
    base_context_id: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    authorized_tool_names: tuple[str, ...] = ()
    task_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunBaseContext:
    """In-memory view of the stable selection saved in a Run checkpoint."""

    context_id: str | None
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class InvocationContext:
    context_id: str
    purpose: ContextPurpose
    payload: dict[str, Any]
    estimated_tokens: int
    token_budget: int
    sections: dict[str, Any]
    included_sources: dict[str, list[str]]
    excluded_sources: list[dict[str, Any]]
    prompt_hash: str
    base_context_id: str | None
