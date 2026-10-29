"""跨 API、Service 和前端使用的领域数据契约。

这里的 Pydantic Model 负责“数据长什么样”，不负责“业务下一步怎么走”。
把契约集中在此处可以让 FastAPI 自动校验输入，并防止 Service 之间传递结构不明的字典。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    AgentRole,
    ArtifactStatus,
    DocumentKind,
    DelegationStatus,
    EngineeringIssueStatus,
    IssueDecisionAction,
    KnowledgeType,
    Mutability,
    ResolutionAction,
    RunStatus,
    RunType,
    Severity,
    TaskStatus,
    ToolRunStatus,
    UserRole,
)


class DocumentInput(BaseModel):
    kind: DocumentKind
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content: str = Field(min_length=1)


class SourceRef(BaseModel):
    document: str
    version: str
    location: str = "document"


class AgentMessage(BaseModel):
    message_type: str
    from_role: str
    to_role: str = "orchestrator"
    subject: str
    content: str
    knowledge_type: KnowledgeType
    sources: list[SourceRef] = Field(default_factory=list)
    confidence_score: int = Field(ge=1, le=10)
    authority_level: str
    requires_user_action: bool = False


class Issue(BaseModel):
    issue_id: str
    title: str
    description: str
    sources: list[SourceRef]
    status: str = "open"
    discussion_round: int = 0


class Finding(BaseModel):
    finding_id: str
    reviewer: str
    title: str
    description: str
    severity: Severity
    sources: list[SourceRef] = Field(default_factory=list)
    status: str = "open"


class CreateDSTaskRequest(BaseModel):
    project_id: str = Field(min_length=1)
    module_name: str = Field(min_length=1)
    documents: list[DocumentInput] = Field(min_length=1)
    disabled_agents: set[str] = Field(default_factory=set)
    custom_reviewers: list[str] = Field(default_factory=list)
    max_discussion_rounds: int = Field(default=2, ge=1, le=3)
    instruction: str = "生成模块 DS"

    @model_validator(mode="after")
    def require_architecture_document(self) -> "CreateDSTaskRequest":
        kinds = {document.kind for document in self.documents}
        if DocumentKind.AS not in kinds:
            raise ValueError("At least one AS document is required for a DS task")
        return self


class UserResolution(BaseModel):
    action: ResolutionAction
    content: str = ""
    evidence_reference: str | None = None

    @model_validator(mode="after")
    def validate_resolution_content(self) -> "UserResolution":
        if self.action == ResolutionAction.PROVIDE_RESOLUTION and not self.content.strip():
            raise ValueError("content is required when providing a resolution")
        return self


class TaskEvent(BaseModel):
    event_type: str
    message: str
    agent_id: str | None = None


class TaskView(BaseModel):
    task_id: str
    project_id: str
    module_name: str
    status: TaskStatus
    issues: list[Issue] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)
    events: list[TaskEvent] = Field(default_factory=list)
    pending_user_request: dict[str, Any] | None = None
    ds_markdown: str | None = None


class ExtractedDocument(BaseModel):
    name: str
    suggested_kind: DocumentKind | None = None
    version: str = "1.0"
    content: str
    size_bytes: int


class ChatHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """网页每次发送消息时提交的完整上下文快照。"""
    session_id: str | None = None
    project_id: str = Field(min_length=1)
    module_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=40)
    documents: list[DocumentInput] = Field(default_factory=list)
    disabled_agents: set[str] = Field(default_factory=set)
    custom_reviewers: list[str] = Field(default_factory=list)
    max_discussion_rounds: int = Field(default=2, ge=1, le=3)
    workspace_id: str | None = None
    ds_markdown: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    user_approved: bool = False
    user_role: UserRole | None = None


class TaskIntent(BaseModel):
    """A derived interpretation of a user turn; the raw request remains immutable."""

    raw_request: str
    goal: str
    deliverables: list[str] = Field(default_factory=list)
    target_module: str | None = None
    known_context: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    candidate_tools: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: int = Field(default=5, ge=1, le=10)


class ToolSelection(BaseModel):
    """主 Agent 第二阶段的输出：选择一个 Tool，并填写其参数。"""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ToolShortlist(BaseModel):
    """轻量 Selector 第一阶段的输出，只含候选名称，不含执行参数。"""
    selected_tools: list[str] = Field(default_factory=list)
    reason: str = ""
    used_fallback: bool = False


class ConversationRoleUpdate(BaseModel):
    """显式切换会话工作角色的 API 请求。"""
    user_role: UserRole


class ConversationView(BaseModel):
    """会话级角色元数据；``primary_agent`` 由后端根据用户角色计算。"""
    session_id: str
    project_id: str
    user_role: UserRole
    primary_agent: AgentRole
    created_at: str
    updated_at: str


class DelegationArtifactRef(BaseModel):
    """专家任务引用的制品名称/路径和版本，不复制制品正文。"""
    path: str
    version: str = ""


class DelegationRequest(BaseModel):
    """主 Agent 发给专家的结构化任务合同。"""
    parent_run_id: str
    from_agent: AgentRole
    to_agent: AgentRole
    task_type: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    artifacts: list[DelegationArtifactRef] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = "结构化专业分析"


class DelegationResult(BaseModel):
    """持久化专家结果；失败时 conclusion 可空，但 status/error 必须可追踪。"""
    task_id: str
    parent_run_id: str
    from_agent: AgentRole
    to_agent: AgentRole
    status: DelegationStatus
    conclusion: str = ""
    evidence: list[str] = Field(default_factory=list)
    artifacts: list[DelegationArtifactRef] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    error: str | None = None
    requires_human_intervention: bool = False
    created_at: str
    updated_at: str


class RunEventView(BaseModel):
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    message: str
    agent_id: str | None = None
    created_at: str


class RunView(BaseModel):
    """统一用户任务视图，关联 DS Task、Patch、ToolRun 和 Delegate。"""
    run_id: str
    session_id: str
    project_id: str
    module_name: str
    workspace_id: str | None = None
    run_type: RunType
    status: RunStatus
    user_instruction: str
    route_reason: str = ""
    primary_agent: AgentRole | None = None
    legacy_task_id: str | None = None
    tool_run_id: str | None = None
    patch_id: str | None = None
    pending_user_request: dict[str, Any] | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    events: list[RunEventView] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RunResumeRequest(BaseModel):
    action: Literal[
        "provide_resolution",
        "accept_assumption",
        "ignore",
        "approve",
        "reject",
        "custom",
        "cancel",
    ]
    content: str = ""
    evidence_reference: str | None = None
    workspace_id: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    session_id: str
    assistant_message: str
    route: str = "orchestrated"
    route_reason: str | None = None
    task: TaskView | None = None
    run: RunView | None = None


class ModelSettingsUpdate(BaseModel):
    """模型设置写入请求；API Key 只用于写入，绝不会出现在 View 中。"""
    enabled: bool
    provider: Literal["openai", "deepseek"] = "openai"
    api_style: Literal["responses", "chat_completions"] = "responses"
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str | None = None
    clear_api_key: bool = False
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    max_output_tokens: int = Field(default=6000, ge=256, le=100_000)
    selector_model: str = Field(default="deepseek-chat", min_length=1)
    selector_max_tools: int = Field(default=6, ge=2, le=20)
    selector_max_output_tokens: int = Field(default=1000, ge=128, le=8000)


class ModelSettingsView(BaseModel):
    """可安全返回前端的模型配置，仅包含 Key 是否存在及掩码。"""
    enabled: bool
    provider: str
    api_style: str
    model: str
    base_url: str
    timeout_seconds: int
    max_output_tokens: int
    selector_model: str
    selector_max_tools: int
    selector_max_output_tokens: int
    api_key_source: str
    has_api_key: bool
    api_key_masked: str | None
    ready: bool


class ModelConnectionResult(BaseModel):
    ok: bool
    message: str
    model: str


class DiagnosticCheck(BaseModel):
    check_id: str
    label: str
    status: Literal["pass", "warning", "error"]
    detail: str
    action: str | None = None


class DiagnosticReport(BaseModel):
    status: Literal["ok", "warning", "error"]
    generated_at: str
    version: str
    checks: list[DiagnosticCheck]


class WorkspaceCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)


class WorkspaceView(BaseModel):
    workspace_id: str
    project_id: str
    display_name: str
    root_path: str
    created_at: str


class ArtifactCreateRequest(BaseModel):
    artifact_type: str = Field(min_length=1, max_length=40)
    relative_path: str = Field(min_length=1, max_length=500)
    content: str
    status: ArtifactStatus = ArtifactStatus.CANDIDATE
    derived_from: list[str] = Field(default_factory=list)


class ArtifactView(BaseModel):
    artifact_id: str
    workspace_id: str
    artifact_type: str
    relative_path: str
    version: int
    content_hash: str
    status: ArtifactStatus
    derived_from: list[str]
    created_at: str


class ToolDefinitionView(BaseModel):
    """模型可见的 Tool 契约和后端权限元数据。

    ``as_function_tool`` 只负责协议转换；是否允许执行仍由 Registry/Service 判断。
    """
    name: str
    description: str
    when_to_use: str = ""
    when_not_to_use: str = ""
    allowed_roles: list[AgentRole]
    mutability: Mutability
    requires_user_approval: bool
    idempotent: bool
    timeout_seconds: int
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    preconditions: list[str] = Field(default_factory=list)
    side_effect: str = "none"
    execution_kind: Literal["orchestration", "engineering"] = "engineering"
    category: str = "common"
    enabled: bool = True
    always_available: bool = False

    def as_function_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": " ".join(
                    part
                    for part in (
                        self.description,
                        f"适用：{self.when_to_use}" if self.when_to_use else "",
                        f"不适用：{self.when_not_to_use}" if self.when_not_to_use else "",
                    )
                    if part
                ),
                "parameters": self.input_schema,
            },
        }


class ToolRunRequest(BaseModel):
    workspace_id: str
    tool_name: str
    role: AgentRole
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_approved: bool = False


class ToolRunView(BaseModel):
    run_id: str
    workspace_id: str
    tool_name: str
    status: ToolRunStatus
    arguments: dict[str, Any]
    command: list[str]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    created_at: str
    updated_at: str


class EngineeringIssueView(BaseModel):
    issue_id: str
    run_id: str
    queue_index: int
    stage: str
    tool: str
    severity: str
    code: str | None = None
    message: str
    file: str | None = None
    line: int | None = None
    raw_log_ref: str | None = None
    fingerprint: str
    status: EngineeringIssueStatus
    user_decision: dict[str, Any] | None = None
    root_cause_analysis: str = ""
    evidence: list[str] = Field(default_factory=list)
    impact: str = ""
    suggestion: str = ""
    confidence: int = Field(default=5, ge=1, le=10)
    verification_scope: list[str] = Field(default_factory=list)


class IssueDecisionRequest(BaseModel):
    action: IssueDecisionAction
    comment: str = ""
    patch_id: str | None = None


class PatchCreateRequest(BaseModel):
    workspace_id: str
    target_path: str = Field(min_length=1, max_length=500)
    after_content: str
    reason: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(default_factory=list)


class PatchDecisionRequest(BaseModel):
    action: Literal["approve", "reject", "custom"]
    comment: str = ""


class PatchView(BaseModel):
    patch_id: str
    workspace_id: str
    target_path: str
    reason: str
    sources: list[str]
    diff: str
    status: str
    created_at: str
    applied_at: str | None = None


class RTLCandidateRequest(BaseModel):
    project_id: str = Field(min_length=1)
    workspace_id: str | None = None
    module_name: str = Field(min_length=1)
    target_path: str | None = None
    ds_markdown: str = Field(min_length=1)
    documents: list[DocumentInput] = Field(default_factory=list)
    instruction: str = "根据已确认 DS 生成 SystemVerilog RTL 候选"


class RTLCandidateResponse(BaseModel):
    workspace: WorkspaceView
    patch: PatchView
    candidate_content: str
    agent_id: str = "design"

