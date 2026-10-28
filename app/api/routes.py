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
)
from app.models.provider import ModelInvocationError, OpenAIModelProvider
from app.services.conversation_service import conversation_service
from app.services.diagnostics_service import diagnostics_service
from app.services.task_service import task_service
from app.services.patch_service import patch_service
from app.services.rtl_service import rtl_service
from app.services.run_service import run_service
from app.services.tool_run_service import tool_run_service
from app.services.workspace_service import artifact_service, workspace_service
from app.tools.registry import tool_registry


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.4.0"}


@router.get("/diagnostics", response_model=DiagnosticReport)
async def diagnostics() -> DiagnosticReport:
    return diagnostics_service.run()


@router.get("/settings/model", response_model=ModelSettingsView)
async def get_model_settings() -> ModelSettingsView:
    return ModelSettingsView.model_validate(model_settings_service.public_view())


@router.put("/settings/model", response_model=ModelSettingsView)
async def update_model_settings(request: ModelSettingsUpdate) -> ModelSettingsView:
    model_settings_service.save(request.model_dump())
    return ModelSettingsView.model_validate(model_settings_service.public_view())


@router.post("/settings/model/test", response_model=ModelConnectionResult)
async def test_model_connection() -> ModelConnectionResult:
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
    try:
        payload = await file.read()
        return extract_document(file.filename or "uploaded-file", payload)
    except DocumentExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await conversation_service.handle(request)


@router.post("/conversations/{session_id}/messages", response_model=ChatResponse)
async def send_conversation_message(session_id: str, request: ChatRequest) -> ChatResponse:
    """Primary UI entrypoint; specialized task endpoints remain compatibility APIs."""
    return await conversation_service.handle(request.model_copy(update={"session_id": session_id}))


@router.get("/runs/{run_id}", response_model=RunView)
async def get_run(run_id: str) -> RunView:
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


@router.get("/tools", response_model=list[ToolDefinitionView])
async def list_tools() -> list[ToolDefinitionView]:
    return tool_registry.list()


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

