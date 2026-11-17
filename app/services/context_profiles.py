"""Data selection and section budgets for each kind of model call."""

from dataclasses import dataclass, field

from app.domain.context import ContextPurpose as Purpose


@dataclass(frozen=True)
class ContextProfile:
    required_sources: tuple[str, ...]
    optional_sources: tuple[str, ...] = ()
    section_budgets: dict[str, int] = field(default_factory=dict)
    max_history_turns: int = 0
    max_observations: int = 0
    version: int = 1

    @property
    def allowed_sources(self) -> tuple[str, ...]:
        return self.required_sources + self.optional_sources

    @property
    def allow_memory(self) -> bool:
        return "long_term_memory" in self.allowed_sources or "related_memories" in self.allowed_sources

    @property
    def allow_rag(self) -> bool:
        return "documents" in self.allowed_sources

    @property
    def allow_config(self) -> bool:
        return any(name in self.allowed_sources for name in ("project_configuration", "current_config", "project_config"))

    @property
    def allow_artifacts(self) -> bool:
        return any(name in self.allowed_sources for name in ("artifacts", "accepted_artifacts", "run_summary", "source"))


_STABLE = ("project_configuration", "history", "long_term_memory", "documents", "context_manifest")
_STABLE_TASK = ("project_configuration", "project_configuration_guidance", "long_term_memory", "documents", "context_manifest")
_CHAT_SCOPE = ("project_id", "module_name", "instruction", "project_configuration_guidance")
_INTENT_SCOPE = ("workspace_id", "has_confirmed_ds")


CONTEXT_PROFILES = {
    Purpose.CHAT_BASE: ContextProfile(("current_message",), _CHAT_SCOPE + _STABLE, max_history_turns=48),
    Purpose.CHAT_RESPONSE: ContextProfile(("current_message",), _CHAT_SCOPE + _STABLE, max_history_turns=48),
    Purpose.CONFIG_EXTRACTION: ContextProfile(
        ("current_request", "current_config", "project_id", "module_name"), ("recent_turns",),
        {"recent_turns": 6_000}, max_history_turns=3,
    ),
    Purpose.INTENT_ENRICHMENT: ContextProfile(("raw_request", "available_tools"), _CHAT_SCOPE + _INTENT_SCOPE + _STABLE,
                                             max_history_turns=48),
    Purpose.TOOL_SELECTOR: ContextProfile(
        ("task_intent", "authorized_tool_metadata", "task_summary", "max_tools", "has_observations"), ("primary_agent",),
        max_observations=1,
    ),
    Purpose.TOOL_CALL: ContextProfile(
        ("raw_request", "task_intent", "already_called_tools"),
        ("previous_observations", "primary_agent") + _CHAT_SCOPE + _INTENT_SCOPE + _STABLE, {"previous_observations": 4_000},
        max_history_turns=48, max_observations=12,
    ),
    Purpose.GLOBAL_PLANNING: ContextProfile(
        ("raw_request", "intent", "catalog", "planning_policy", "workflow_contract"), ("source",) + _STABLE,
        max_history_turns=48,
    ),
    Purpose.REFLECTION: ContextProfile(
        ("contract", "task_state", "budget", "visible_evidence", "verification_feedback", "artifacts"),
        ("previous_attempt_feedback", "plan_tasks", "observations", "artifact_details") + _STABLE,
        {"artifact_details": 4_000, "observations": 4_000}, max_history_turns=48, max_observations=12,
    ),
    Purpose.REPLANNING: ContextProfile(
        ("plan", "trigger", "catalog", "policy", "original_goal", "tasks"), ("accepted_artifacts",),
        max_observations=3,
    ),
    Purpose.SPECIALIST_ANALYSIS: ContextProfile(
        ("instruction", "source"), ("artifacts", "long_term_memory", "documents"),
    ),
    Purpose.DS_GENERATION: ContextProfile(
        ("instruction", "design_analysis", "user_resolution"), _STABLE_TASK,
    ),
    Purpose.RTL_GENERATION: ContextProfile(("instruction", "ds_markdown"), _STABLE_TASK),
    Purpose.VERIFICATION: ContextProfile(
        ("instruction", "source", "design_analysis"), ("artifacts", "long_term_memory", "documents"),
    ),
    Purpose.INDEPENDENT_REVIEW: ContextProfile(
        ("instruction", "ds_markdown", "review_category", "custom_prompt"),
        ("project_configuration", "project_configuration_guidance", "documents"),
    ),
    Purpose.MEMORY_EXTRACTION: ContextProfile(
        ("run_id", "project_id", "module_name", "user_request", "assistant_response", "run_summary", "project_config"),
        ("related_memories",),
    ),
    Purpose.CONVERSATION_SUMMARY: ContextProfile(("turn",)),
    Purpose.SUMMARY_COMPRESSION: ContextProfile(("summary",)),
    Purpose.JSON_REPAIR: ContextProfile(("raw_output", "expected_shape")),
    Purpose.MODEL_HEALTH_CHECK: ContextProfile(("probe",)),
}
