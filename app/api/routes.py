"""FastAPI HTTP 边界。

路由层只做四件事：接收/校验 HTTP 输入、调用 Service、转换已知异常、返回响应模型。
它不应该包含 Agent Prompt、SQL、文件写入或工作流状态转换。这样同一 Service 将来
可以被网页、CLI 或测试复用，而不会依赖 FastAPI Request 对象。
"""

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from app.documents.extractor import DocumentExtractionError, extract_document
from app.config.model_settings import model_settings_service
from app.domain.models import (
    ChatRequest,
    ChatResponse,
    CreateDSTaskRequest,
    DiagnosticReport,
    ExtractedDocument,
    ModelConnectionResult,
    ModelSettingsUpdate,
    ModelSettingsView,
    ArtifactCreateRequest,
    ArtifactView,
    EngineeringIssueView,
    IssueDecisionRequest,
    PatchCreateRequest,
    PatchDecisionRequest,
    PatchView,
    RTLCandidateRequest,
    RTLCandidateResponse,
    RunEventView,
    RunResumeRequest,
    RunView,
    TaskView,
    ToolDefinitionView,
    ToolRunRequest,
    ToolRunView,
    UserResolution,
    WorkspaceCreateRequest,
    WorkspaceView,
    ConversationRoleUpdate,
    ConversationView,
    DelegationResult,
)
from app.domain.enums import AgentRole
from app.models.provider import ModelInvocationError, OpenAIModelProvider
from app.services.conversation_service import conversation_service
from app.services.diagnostics_service import diagnostics_service
from app.services.task_service import task_service
from app.services.patch_service import patch_service
from app.services.rtl_service import rtl_service
from app.services.run_service import run_service
from app.services.tool_run_service import tool_run_service
from app.services.workspace_service import artifact_service, workspace_service
from app.services.session_service import session_service
from app.services.delegation_service import delegation_service
from app.tools.registry import tool_registry


router = APIRouter()


# ---------------------------------------------------------------------------
# 系统状态、模型配置和上传文档
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict[str, str]:
    """最轻量存活检查，不访问模型或 EDA 工具。"""
    return {"status": "ok", "version": "0.5.0"}


@router.get("/diagnostics", response_model=DiagnosticReport)
async def diagnostics() -> DiagnosticReport:
    """运行本机自检；与 health 不同，它会检查数据库、配置和工具环境。"""
    return diagnostics_service.run()


@router.get("/settings/model", response_model=ModelSettingsView)
async def get_model_settings() -> ModelSettingsView:
    """读取脱敏设置，响应中不会包含完整 API Key。"""
    return ModelSettingsView.model_validate(model_settings_service.public_view())


@router.put("/settings/model", response_model=ModelSettingsView)
async def update_model_settings(request: ModelSettingsUpdate) -> ModelSettingsView:
    """保存设置后重新读取有效配置，确保前端看到最终合并结果。"""
    model_settings_service.save(request.model_dump())
    return ModelSettingsView.model_validate(model_settings_service.public_view())


@router.post("/settings/model/test", response_model=ModelConnectionResult)
async def test_model_connection() -> ModelConnectionResult:
    """发送最小真实模型请求；只证明连接可用，不证明所有任务输出质量。"""
    settings = model_settings_service.effective()
    if not settings.api_key:
        raise HTTPException(status_code=400, detail="请先保存 API Key")
    try:
        OpenAIModelProvider(settings).test_connection()
    except ModelInvocationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ModelConnectionResult(
        ok=True,
        message="模型连接成功",
        model=settings.model,
    )


@router.post("/documents/extract", response_model=ExtractedDocument)
async def extract_uploaded_document(file: UploadFile = File(...)) -> ExtractedDocument:
    """读取上传文件并抽取文本；上传本身不会自动启动 DS Workflow。"""
    try:
        payload = await file.read()
        return extract_document(file.filename or "uploaded-file", payload)
    except DocumentExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()


# ---------------------------------------------------------------------------
# 统一对话、会话角色、Run 和 Delegate
# ---------------------------------------------------------------------------
@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """兼容入口：没有 session_id 时由后端创建新会话。"""
    try:
        return await conversation_service.handle(request)
    except ValueError as exc:
        if str(exc) == "Conversation belongs to a different project":
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


@router.post("/conversations/{session_id}/messages", response_model=ChatResponse)
async def send_conversation_message(session_id: str, request: ChatRequest) -> ChatResponse:
    """正式网页主入口；路径中的 session_id 覆盖请求体，防止两者不一致。"""
    try:
        return await conversation_service.handle(
            request.model_copy(update={"session_id": session_id})
        )
    except ValueError as exc:
        if str(exc) == "Conversation belongs to a different project":
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


@router.get("/conversations/{session_id}", response_model=ConversationView)
async def get_conversation(session_id: str) -> ConversationView:
    """查询当前工作角色和对应主 Agent，不返回聊天正文。"""
    conversation = session_service.get(session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.put("/conversations/{session_id}/role", response_model=ConversationView)
async def update_conversation_role(
    session_id: str, request: ConversationRoleUpdate
) -> ConversationView:
    """显式切换工作角色；下一条聊天请求还会执行上下文隔离。"""
    try:
        return session_service.set_role(session_id, request.user_role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


@router.get("/runs/{run_id}", response_model=RunView)
async def get_run(run_id: str) -> RunView:
    """查询统一 Run；Service 会同步关联 ToolRun 的最新终态。"""
    run = run_service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}/events", response_model=list[RunEventView])
async def get_run_events(run_id: str) -> list[RunEventView]:
    run = run_service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.events


@router.get("/runs/{run_id}/delegations", response_model=list[DelegationResult])
async def get_run_delegations(run_id: str) -> list[DelegationResult]:
    """查询一个 Run 下全部专家任务，便于 UI 或调试工具展示协作证据。"""
    if run_service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return delegation_service.list_for_run(run_id)


@router.post("/runs/{run_id}/resume", response_model=RunView)
async def resume_run(run_id: str, request: RunResumeRequest) -> RunView:
    try:
        return await run_service.resume(run_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel", response_model=RunView)
async def cancel_run(run_id: str) -> RunView:
    try:
        return await run_service.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 专业对象兼容接口
# 正式网页优先走 /chat；这些接口服务于测试、调试和外部系统集成。
# ---------------------------------------------------------------------------
@router.post("/tasks/ds", response_model=TaskView, status_code=status.HTTP_201_CREATED)
async def create_ds_task(request: CreateDSTaskRequest) -> TaskView:
    return await task_service.create_ds_task(request)


@router.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(task_id: str) -> TaskView:
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/resume", response_model=TaskView)
async def resume_task(task_id: str, resolution: UserResolution) -> TaskView:
    try:
        return await task_service.resume_task(task_id, resolution)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Workspace / Artifact：受控文件系统边界
# ---------------------------------------------------------------------------
@router.post("/workspaces", response_model=WorkspaceView, status_code=status.HTTP_201_CREATED)
async def create_workspace(request: WorkspaceCreateRequest) -> WorkspaceView:
    return workspace_service.create(request)


@router.get("/workspaces", response_model=list[WorkspaceView])
async def list_workspaces(project_id: str | None = Query(default=None)) -> list[WorkspaceView]:
    return workspace_service.list(project_id)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceView)
async def get_workspace(workspace_id: str) -> WorkspaceView:
    workspace = workspace_service.get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@router.post(
    "/workspaces/{workspace_id}/artifacts",
    response_model=ArtifactView,
    status_code=status.HTTP_201_CREATED,
)
async def create_artifact(workspace_id: str, request: ArtifactCreateRequest) -> ArtifactView:
    try:
        return artifact_service.create(workspace_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/artifacts", response_model=list[ArtifactView])
async def list_artifacts(workspace_id: str) -> list[ArtifactView]:
    if workspace_service.get(workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return artifact_service.list(workspace_id)


# ---------------------------------------------------------------------------
# ToolRun / Issue：真实本地工具执行与逐条错误处理
# ---------------------------------------------------------------------------
@router.get("/tools", response_model=list[ToolDefinitionView])
async def list_tools(role: AgentRole | None = Query(default=None)) -> list[ToolDefinitionView]:
    """不传 role 返回诊断用全量目录；传 role 返回该角色实际可见 Tool。"""
    return tool_registry.for_role(role) if role else tool_registry.list()


@router.post("/tool-runs", response_model=ToolRunView, status_code=status.HTTP_202_ACCEPTED)
async def start_tool_run(request: ToolRunRequest) -> ToolRunView:
    try:
        return tool_run_service.start(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tool-runs/{run_id}", response_model=ToolRunView)
async def get_tool_run(run_id: str) -> ToolRunView:
    run = tool_run_service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Tool run not found")
    return run


@router.get("/tool-runs/{run_id}/issues", response_model=list[EngineeringIssueView])
async def list_tool_issues(run_id: str) -> list[EngineeringIssueView]:
    if tool_run_service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Tool run not found")
    return tool_run_service.issues(run_id)


@router.get("/tool-runs/{run_id}/issues/next", response_model=EngineeringIssueView | None)
async def get_next_tool_issue(run_id: str) -> EngineeringIssueView | None:
    if tool_run_service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Tool run not found")
    return tool_run_service.next_issue(run_id)


@router.post("/issues/{issue_id}/decision", response_model=EngineeringIssueView)
async def decide_issue(issue_id: str, request: IssueDecisionRequest) -> EngineeringIssueView:
    try:
        return tool_run_service.decide(issue_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Issue not found") from exc


# ---------------------------------------------------------------------------
# Patch / RTL Candidate：所有写入先产生 Diff，用户批准后才应用
# ---------------------------------------------------------------------------
@router.post("/patches", response_model=PatchView, status_code=status.HTTP_201_CREATED)
async def create_patch(request: PatchCreateRequest) -> PatchView:
    try:
        return patch_service.create(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/patches/{patch_id}", response_model=PatchView)
async def get_patch(patch_id: str) -> PatchView:
    patch = patch_service.get(patch_id)
    if patch is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    return patch


@router.post("/patches/{patch_id}/decision", response_model=PatchView)
async def decide_patch(patch_id: str, request: PatchDecisionRequest) -> PatchView:
    try:
        return patch_service.decide(patch_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Patch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rtl/candidates", response_model=RTLCandidateResponse, status_code=status.HTTP_201_CREATED)
async def create_rtl_candidate(request: RTLCandidateRequest) -> RTLCandidateResponse:
    try:
        return rtl_service.create_candidate(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

