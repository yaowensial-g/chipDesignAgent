import difflib
from uuid import uuid4

from app.domain.models import PatchCreateRequest, PatchDecisionRequest, PatchView
from app.services.workspace_service import WorkspaceService, utc_now, workspace_service
from app.storage.database import Database, database


class PatchService:
    def __init__(
        self,
        workspaces: WorkspaceService = workspace_service,
        db: Database = database,
    ) -> None:
        self.workspaces = workspaces
        self.db = db

    def create(self, request: PatchCreateRequest) -> PatchView:
        target = self.workspaces.resolve_path(request.workspace_id, request.target_path)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                request.after_content.splitlines(keepends=True),
                fromfile=f"a/{request.target_path}",
                tofile=f"b/{request.target_path}",
            )
        )
        patch_id = str(uuid4())
        created_at = utc_now()
        self.db.execute(
            "INSERT INTO patches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                patch_id,
                request.workspace_id,
                request.target_path,
                before,
                request.after_content,
                request.reason,
                self.db.dumps(request.sources),
                diff,
                "patch_ready",
                created_at,
            ),
        )
        return self.get(patch_id)  # type: ignore[return-value]

    def get(self, patch_id: str) -> PatchView | None:
        row = self.db.fetch_one("SELECT * FROM patches WHERE patch_id = ?", (patch_id,))
        if not row:
            return None
        return PatchView(
            patch_id=row["patch_id"],
            workspace_id=row["workspace_id"],
            target_path=row["target_path"],
            reason=row["reason"],
            sources=self.db.loads(row["sources_json"], []),
            diff=row["diff"],
            status=row["status"],
            created_at=row["created_at"],
            applied_at=row["applied_at"],
        )

    def decide(self, patch_id: str, decision: PatchDecisionRequest) -> PatchView:
        row = self.db.fetch_one("SELECT * FROM patches WHERE patch_id = ?", (patch_id,))
        if not row:
            raise KeyError(patch_id)
        if row["status"] not in {"patch_ready", "custom_requested"}:
            raise ValueError(f"Patch is not awaiting a decision: {row['status']}")
        if decision.action == "reject":
            self.db.execute(
                "UPDATE patches SET status = ? WHERE patch_id = ?", ("rejected", patch_id)
            )
        elif decision.action == "custom":
            self.db.execute(
                "UPDATE patches SET status = ? WHERE patch_id = ?",
                ("custom_requested", patch_id),
            )
        else:
            target = self.workspaces.resolve_path(row["workspace_id"], row["target_path"])
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != row["before_content"]:
                raise ValueError("Target changed after patch creation; create a new patch")
            self.workspaces.write_text(
                row["workspace_id"], row["target_path"], row["after_content"]
            )
            self.db.execute(
                "UPDATE patches SET status = ?, applied_at = ? WHERE patch_id = ?",
                ("applied", utc_now(), patch_id),
            )
        return self.get(patch_id)  # type: ignore[return-value]


patch_service = PatchService()
