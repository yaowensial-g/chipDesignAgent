from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ToolSelection


Key = Annotated[str, Field(min_length=1, max_length=120)]
Text = Annotated[str, Field(min_length=1, max_length=8_000)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Positive = Annotated[int, Field(ge=1)]
Count = Annotated[int, Field(ge=0)]

TASK_CONTROLS = {
    "submit_task_output": "submit",
    "ask_user": "ask_user",
    "report_task_failure": "fail",
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunInput(ContractModel):
    kind: Literal["run_input"]
    key: Key


class TaskOutputRef(ContractModel):
    kind: Literal["task_output"]
    task_id: Key
    output_key: Key


class InputSpec(ContractModel):
    key: Key
    artifact_type: Key
    source: Annotated[RunInput | TaskOutputRef, Field(discriminator="kind")]
    required: bool = True


class OutputSpec(ContractModel):
    key: Key
    artifact_type: Key
    media_type: Key
    schema_id: Key | None = None
    required: bool = True


class Criterion(ContractModel):
    criterion_id: Key
    description: Text
    rule_id: Key
    output_keys: tuple[Key, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class TaskConstraints(ContractModel):
    read_only: bool = True
    allowed_mutability: tuple[Mutability, ...] = (Mutability.READ, Mutability.ANALYSIS)
    user_constraint_ids: tuple[Key, ...] = ()
    notes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def check_mutability(self) -> "TaskConstraints":
        allowed = set(self.allowed_mutability)
        if allowed & {Mutability.FORMAL_WRITE, Mutability.EXTERNAL_WRITE}:
            raise ValueError("Formal and external writes require a separate professional endpoint")
        if self.read_only and allowed - {Mutability.READ, Mutability.ANALYSIS}:
            raise ValueError("Read-only tasks cannot request write or execution actions")
        return self


class ExecutionPolicy(ContractModel):
    max_action_steps: int = Field(default=8, ge=0)
    max_repair_attempts: int = Field(default=3, ge=0)
    max_decisions: int = Field(default=16, ge=1)


class RunExecutionPolicy(ContractModel):
    max_plan_tasks: int = Field(default=8, ge=1)
    max_replans: int = Field(default=2, ge=0)


class TaskContract(ContractModel):
    task_id: Key
    title: Text
    goal: Text
    assigned_agent: AgentRole
    capability_requirements: Annotated[tuple[Key, ...], Field(min_length=1)]
    tool_hints: tuple[Key, ...] = ()
    inputs: tuple[InputSpec, ...]
    outputs: Annotated[tuple[OutputSpec, ...], Field(min_length=1)]
    constraints: TaskConstraints
    depends_on: tuple[Key, ...]
    acceptance_criteria: Annotated[tuple[Criterion, ...], Field(min_length=1)]
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)


class TaskPlan(ContractModel):
    plan_id: Key
    run_id: Key
    goal: Text
    tasks: Annotated[tuple[TaskContract, ...], Field(min_length=1)]
    success_criteria: Annotated[tuple[Criterion, ...], Field(min_length=1)]
    final_task_id: Key
    created_at: AwareDatetime
    version: Positive
    status: Literal["active", "superseded", "closed"]


class TaskPlanDraft(ContractModel):
    """Task IDs in a draft are local keys. The service resolves them before activation."""

    goal: Text
    tasks: Annotated[tuple[TaskContract, ...], Field(min_length=1)]
    success_criteria: Annotated[tuple[Criterion, ...], Field(min_length=1)]
    final_task_id: Key


class PlanningResult(ContractModel):
    status: Literal["planned", "waiting_for_user", "capability_gap", "failed"]
    plan: TaskPlan | None = None
    message: Text
    diagnostics: tuple[Key, ...] = ()


class EvidenceRef(ContractModel):
    kind: Literal["artifact", "document_version", "chunk", "observation", "tool_run", "message"]
    ref_id: Key
    locator: Text | None = None


class Observation(ContractModel):
    observation_id: Key
    run_id: Key
    task_id: Key
    generation: Positive
    action_id: Key
    tool_name: Key
    status: Literal["succeeded", "failed", "waiting", "denied", "cancelled", "unknown"]
    summary: Text
    evidence: tuple[EvidenceRef, ...] = ()
    artifact_ids: tuple[Key, ...] = ()
    error: Text | None = None
    created_at: AwareDatetime


class ReflectionResult(ContractModel):
    progress: Literal["starting", "in_progress", "ready_to_submit", "blocked"]
    progress_summary: Text
    satisfied_criteria: tuple[Key, ...]
    unmet_criteria: tuple[Key, ...]
    issues: tuple[Text, ...]
    decision: Literal["continue", "submit", "ask_user", "fail"]
    reason: Text
    evidence: tuple[EvidenceRef, ...] = ()

    def validate_decision(
        self, contract: TaskContract, tool_name: str, visible_evidence: tuple[EvidenceRef, ...],
    ) -> None:
        reported = (*self.satisfied_criteria, *self.unmet_criteria)
        expected = {criterion.criterion_id for criterion in contract.acceptance_criteria}
        if set(reported) != expected or len(reported) != len(expected):
            raise ValueError("Reflection must classify each criterion once")
        if self.decision != TASK_CONTROLS.get(tool_name, "continue"):
            raise ValueError("Reflection decision does not match the selected tool")
        if any(item not in visible_evidence for item in self.evidence):
            raise ValueError("Reflection refers to unavailable evidence")


class ReflectionRecord(ContractModel):
    reflection_id: Key
    run_id: Key
    task_id: Key
    generation: Positive
    decision_sequence: Positive
    action_id: Key
    observation_ids: tuple[Key, ...] = ()
    verification_id: Key | None = None
    result: ReflectionResult
    created_at: AwareDatetime


class ProfessionalRef(ContractModel):
    kind: Literal["ds_task", "patch", "tool_run", "delegation"]
    object_id: Key


class ActionApproval(ContractModel):
    run_id: Key
    task_id: Key
    generation: Positive
    action_id: Key
    workspace_id: Key
    tool_name: Key
    argument_hash: Hash
    input_hash: Hash | None = None


class ActionWaitRequest(ContractModel):
    kind: Literal["clarification", "ds_resolution", "tool_approval", "patch_approval"]
    message: Text
    required_input_keys: tuple[Key, ...] = ()
    approval: ActionApproval | None = None


class TaskActionResult(ContractModel):
    outcome: Literal[
        "observed", "waiting_for_user", "waiting_for_approval",
        "awaiting_external", "failed",
    ]
    summary: Text
    evidence: tuple[EvidenceRef, ...] = ()
    candidate_artifact_ids: tuple[Key, ...] = ()
    professional_refs: tuple[ProfessionalRef, ...] = ()
    wait: ActionWaitRequest | None = None
    error_code: Key | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> "TaskActionResult":
        wait_kinds = {
            "waiting_for_user": {"clarification", "ds_resolution"},
            "waiting_for_approval": {"tool_approval", "patch_approval"},
        }
        if self.outcome in wait_kinds:
            if self.wait is None or self.wait.kind not in wait_kinds[self.outcome]:
                raise ValueError("Action outcome needs a matching wait request")
        elif self.wait is not None:
            raise ValueError("Only a waiting action can carry a wait request")
        if self.outcome == "awaiting_external" and not self.professional_refs:
            raise ValueError("External work needs a professional object reference")
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("Only failed actions must carry an error code")
        return self


class TaskState(ContractModel):
    run_id: Key
    task_id: Key
    generation: Positive
    status: Literal[
        "pending", "ready", "running", "waiting_for_user", "waiting_for_approval",
        "completed", "failed", "blocked", "cancelled",
    ]
    phase: Literal["selecting", "dispatching", "awaiting_external", "verifying", "repairing"] | None = None
    current_iteration: Count = 0
    action_count: Count = 0
    repair_attempts: Count = 0
    observations: tuple[Observation, ...] = ()
    artifact_ids: tuple[Key, ...] = ()
    wait_id: Key | None = None
    failure_reason: Text | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None


class ContentRef(ContractModel):
    backend: Literal["managed_blob"]
    object_key: Hash
    media_type: Key
    byte_size: Count


class Artifact(ContractModel):
    artifact_id: Key
    run_id: Key
    name: Key
    artifact_type: Key
    source_task_id: Key
    source_generation: Positive
    source_action_id: Key
    output_key: Key
    version: Positive
    content_hash: Hash
    content_ref: ContentRef
    status: Literal["staged", "verified", "rejected", "superseded", "invalidated"]
    evidence: tuple[EvidenceRef, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: AwareDatetime


class VerificationResult(ContractModel):
    passed: bool
    failed_criteria: tuple[Key, ...] = ()
    repairable: bool = False
    suggestions: tuple[Text, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()


class RTLSourceSnapshot(ContractModel):
    relative_path: Annotated[str, Field(min_length=1, max_length=500)]
    byte_size: Positive
    line_count: Positive
    content_hash: Hash
    content_ref: Key


class RTLSourceEvidence(ContractModel):
    snapshot_ref: Key
    content_hash: Hash
    start_line: Positive
    end_line: Positive

    def matches(self, snapshot: RTLSourceSnapshot) -> bool:
        return (
            self.snapshot_ref == snapshot.content_ref
            and self.content_hash == snapshot.content_hash
            and self.start_line <= self.end_line <= snapshot.line_count
        )


class RTLFinding(ContractModel):
    statement: Text
    evidence: Annotated[tuple[RTLSourceEvidence, ...], Field(min_length=1)]


class RTLDesignAnalysis(ContractModel):
    summary: Text
    findings: Annotated[tuple[RTLFinding, ...], Field(min_length=1)]


class RTLVerificationAssessment(ContractModel):
    summary: Text
    impacts: Annotated[tuple[RTLFinding, ...], Field(min_length=1)]
    recommended_checks: Annotated[tuple[RTLFinding, ...], Field(min_length=1)]


class ArtifactPayload(ContractModel):
    artifact: Artifact
    content: bytes


class TaskActionContext(ContractModel):
    """Resolved by the backend before tool selection or dispatch."""

    run_id: Key
    generation: Positive
    action_id: Key
    contract: TaskContract
    primary_agent: AgentRole
    project_id: Key
    module_name: Key
    workspace_id: Key | None = None
    permitted_tools: tuple[Key, ...] = ()
    allowed_rtl_paths: tuple[str, ...] = ()
    lint_top_module: Key | None = None
    inputs: tuple[ArtifactPayload, ...] = ()
    action_count: Count = 0


class AuthorizedAction(ContractModel):
    selection: ToolSelection
    argument_hash: Hash
    run_id: Key
    task_id: Key
    generation: Positive
    action_id: Key
    approval: ActionApproval | None = None


class LintResult(ContractModel):
    tool_run_id: Key
    snapshot_ref: Key
    content_hash: Hash
    top_module: Key
    version: Text
    exit_code: int
    stdout_hash: Hash
    stderr_hash: Hash
