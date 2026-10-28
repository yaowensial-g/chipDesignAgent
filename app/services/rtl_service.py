from app.agents.runtime import runtime
from app.domain.models import (
    PatchCreateRequest,
    RTLCandidateRequest,
    RTLCandidateResponse,
    WorkspaceCreateRequest,
)
from app.services.patch_service import PatchService, patch_service
from app.services.workspace_service import WorkspaceService, workspace_service


class RTLService:
    def __init__(
        self,
        workspaces: WorkspaceService = workspace_service,
        patches: PatchService = patch_service,
    ) -> None:
        self.workspaces = workspaces
        self.patches = patches

    def create_candidate(self, request: RTLCandidateRequest) -> RTLCandidateResponse:
        workspace = (
            self.workspaces.get(request.workspace_id)
            if request.workspace_id
            else self.workspaces.create(
                WorkspaceCreateRequest(
                    project_id=request.project_id,
                    display_name=f"{request.project_id} RTL Workspace",
                )
            )
        )
        if workspace is None:
            raise KeyError(f"Workspace not found: {request.workspace_id}")
        target_path = request.target_path or f"rtl/{request.module_name}.sv"
        payload = request.model_dump(mode="json")
        payload["workspace_id"] = workspace.workspace_id
        candidate = runtime.generate_rtl(payload)
        patch = self.patches.create(
            PatchCreateRequest(
                workspace_id=workspace.workspace_id,
                target_path=target_path,
                after_content=candidate,
                reason=request.instruction,
                sources=["confirmed_ds", *[doc.name for doc in request.documents]],
            )
        )
        return RTLCandidateResponse(
            workspace=workspace,
            patch=patch,
            candidate_content=candidate,
        )


rtl_service = RTLService()
