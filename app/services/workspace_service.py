"""隔离 Workspace 与版本化 Artifact 服务。

所有 Agent/Tool 文件访问都应先转换成 Workspace 内的相对路径。这个模块负责阻止
绝对路径和 ``..`` 路径穿越，并使用临时文件替换完成较安全的文本写入。
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.models import (
    ArtifactCreateRequest,
    ArtifactView,
    WorkspaceCreateRequest,
    WorkspaceView,
)
from app.storage.database import Database, PROJECT_ROOT, database


DEFAULT_WORKSPACE_ROOT = PROJECT_ROOT / "data" / "workspaces"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceService:
    """创建工作区并提供安全路径解析、原子文本写入。"""
    def __init__(
        self,
        db: Database = database,
        root: Path = DEFAULT_WORKSPACE_ROOT,
    ) -> None:
        self.db = db
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, request: WorkspaceCreateRequest) -> WorkspaceView:
        """创建独立目录和约定子目录，并把绝对根路径登记到数据库。"""
        workspace_id = str(uuid4())
        # project_id 可能含中文、空格或路径字符，先转换成安全目录片段；
        # UUID 后缀保证同项目可以创建多个互不覆盖的 Workspace。
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", request.project_id).strip("-._") or "project"
        path = (self.root / f"{slug[:48]}-{workspace_id[:8]}").resolve()
        self._assert_within_root(path)
        path.mkdir(parents=True, exist_ok=False)
        for directory in ("rtl", "dv", "constraints", "candidates", "artifacts", "logs"):
            (path / directory).mkdir()
        created_at = utc_now()
        display_name = request.display_name or request.project_id
        self.db.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)",
            (workspace_id, request.project_id, display_name, str(path), created_at),
        )
        return WorkspaceView(
            workspace_id=workspace_id,
            project_id=request.project_id,
            display_name=display_name,
            root_path=str(path),
            created_at=created_at,
        )

    def get(self, workspace_id: str) -> WorkspaceView | None:
        row = self.db.fetch_one(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        )
        return WorkspaceView(**row) if row else None

    def list(self, project_id: str | None = None) -> list[WorkspaceView]:
        if project_id:
            rows = self.db.fetch_all(
                "SELECT * FROM workspaces WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        else:
            rows = self.db.fetch_all("SELECT * FROM workspaces ORDER BY created_at DESC")
        return [WorkspaceView(**row) for row in rows]

    def resolve_path(self, workspace_id: str, relative_path: str) -> Path:
        """把相对路径解析到 Workspace 内，并拒绝绝对路径与目录穿越。

        ``Path.resolve`` 会消除 ``..``，随后再检查目标是否仍是 root 的后代。
        这是所有文件 Tool 都必须经过的核心安全边界。
        """
        workspace = self.get(workspace_id)
        if workspace is None:
            raise KeyError(f"Workspace not found: {workspace_id}")
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ValueError("Workspace path must be relative")
        target = (Path(workspace.root_path) / relative).resolve()
        root = Path(workspace.root_path).resolve()
        if target != root and root not in target.parents:
            raise ValueError("Path escapes the workspace")
        return target

    def write_text(self, workspace_id: str, relative_path: str, content: str) -> Path:
        """先写同目录临时文件，再 replace 目标，降低中途崩溃产生半文件的风险。"""
        target = self.resolve_path(workspace_id, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
        return target

    def _assert_within_root(self, path: Path) -> None:
        if path != self.root and self.root not in path.parents:
            raise ValueError("Workspace path escapes configured root")


class ArtifactService:
    """写入带版本、Hash、来源和状态的工程制品记录。"""
    def __init__(
        self,
        workspace_service: WorkspaceService,
        db: Database = database,
    ) -> None:
        self.workspaces = workspace_service
        self.db = db

    def create(self, workspace_id: str, request: ArtifactCreateRequest) -> ArtifactView:
        """保存制品内容，并为同一路径分配递增版本号。"""
        self.workspaces.write_text(workspace_id, request.relative_path, request.content)
        previous = self.db.fetch_one(
            "SELECT MAX(version) AS version FROM artifacts WHERE workspace_id = ? AND relative_path = ?",
            (workspace_id, request.relative_path),
        )
        version = int(previous["version"] or 0) + 1
        artifact_id = str(uuid4())
        # Hash 用于判断内容是否变化和提供审计标识，不替代版本号或数字签名。
        content_hash = hashlib.sha256(request.content.encode("utf-8")).hexdigest()
        created_at = utc_now()
        self.db.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                workspace_id,
                request.artifact_type,
                request.relative_path,
                version,
                content_hash,
                request.status.value,
                self.db.dumps(request.derived_from),
                created_at,
            ),
        )
        return ArtifactView(
            artifact_id=artifact_id,
            workspace_id=workspace_id,
            artifact_type=request.artifact_type,
            relative_path=request.relative_path,
            version=version,
            content_hash=content_hash,
            status=request.status,
            derived_from=request.derived_from,
            created_at=created_at,
        )

    def list(self, workspace_id: str) -> list[ArtifactView]:
        rows = self.db.fetch_all(
            "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        )
        return [
            ArtifactView(
                artifact_id=row["artifact_id"],
                workspace_id=row["workspace_id"],
                artifact_type=row["artifact_type"],
                relative_path=row["relative_path"],
                version=row["version"],
                content_hash=row["content_hash"],
                status=row["status"],
                derived_from=self.db.loads(row["derived_from_json"], []),
                created_at=row["created_at"],
            )
            for row in rows
        ]


workspace_service = WorkspaceService()
artifact_service = ArtifactService(workspace_service)
