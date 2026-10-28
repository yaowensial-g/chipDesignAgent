from enum import Enum


class DocumentKind(str, Enum):
    FS = "FS"
    AS = "AS"
    ICD = "ICD"


class TaskStatus(str, Enum):
    REQUESTED = "requested"
    PLANNING = "planning"
    EXECUTING = "executing"
    INTERNAL_DISCUSSION = "internal_discussion"
    WAITING_FOR_USER = "waiting_for_user"
    REVIEWING = "reviewing"
    REWORKING = "reworking"
    DELIVERED = "delivered"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunType(str, Enum):
    ORCHESTRATION = "orchestration"
    CHAT = "chat"
    DS_DESIGN = "ds_design"
    RTL_IMPLEMENTATION = "rtl_implementation"
    SIMULATION = "simulation"
    LINT = "lint"
    CDC = "cdc"
    SYNTHESIS = "synthesis"
    STA = "sta"
    TEST_EXECUTION = "test_execution"


class RunStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeType(str, Enum):
    FACT = "fact"
    INFERENCE = "inference"
    SUGGESTION = "suggestion"
    ASSUMPTION = "assumption"
    DECISION = "decision"
    QUESTION = "question"
    FINDING = "finding"


class Severity(str, Enum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"
    QUESTION = "question"


class ResolutionAction(str, Enum):
    PROVIDE_RESOLUTION = "provide_resolution"
    ACCEPT_ASSUMPTION = "accept_assumption"
    IGNORE = "ignore"
    CANCEL = "cancel"


class AgentRole(str, Enum):
    ARCHITECTURE = "architecture"
    DESIGN = "design"
    VERIFICATION = "verification"
    REVIEWER = "reviewer"
    ORCHESTRATOR = "orchestrator"


class Mutability(str, Enum):
    READ = "read"
    ANALYSIS = "analysis"
    CANDIDATE_WRITE = "candidate_write"
    EXECUTION = "execution"
    FORMAL_WRITE = "formal_write"
    EXTERNAL_WRITE = "external_write"


class ArtifactStatus(str, Enum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class ToolRunStatus(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    PARSING = "parsing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class EngineeringIssueStatus(str, Enum):
    DETECTED = "detected"
    ANALYZING = "analyzing"
    AWAITING_USER = "awaiting_user"
    APPROVED = "approved"
    SKIPPED = "skipped"
    CUSTOM_REQUESTED = "custom_requested"
    PATCH_READY = "patch_ready"
    APPLIED = "applied"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    REOPENED = "reopened"


class IssueDecisionAction(str, Enum):
    APPROVE = "approve"
    SKIP = "skip"
    CUSTOM = "custom"
    EXPLAIN = "explain"

