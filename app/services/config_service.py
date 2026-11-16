"""Workspace-backed project specification and Run-bound snapshots."""

from collections.abc import Callable
from pathlib import Path

from app.domain.design_config import (
    ConfigEvidence, ConfigStatus, ConfigUpdate, DesignConfigItem, ModuleDesignConfig,
    ProjectDesignConfig, normalize_module, same_config_value,
)
from app.domain.models import ConversationMessageView, WorkspaceCreateRequest, WorkspaceView
from app.services.token_service import TokenCounter
from app.services.workspace_service import DESIGN_CONFIG_PATH, WorkspaceService, workspace_service
from app.storage.database import Database, database


ConfigProofs = dict[str, tuple[ConfigStatus, list[ConfigEvidence]]]
_STATUS_RANK = {"pending": 0, "implemented": 1, "verified": 2}


class ConfigRevisionConflict(ValueError):
    """The configuration item changed after the extractor read it."""


def config_context(
    config: ProjectDesignConfig, module_name: str, query: str, tokens: TokenCounter, limit: int,
) -> tuple[dict, int]:
    """Select complete configuration items and retain the revision even when some do not fit."""
    name = normalize_module(module_name)
    module = config.modules.get(name)
    if module is None:
        return {}, 0
    items = list(module.items())
    context: dict = {"module": name, "revision": config.revision, "parameters": {}, "instances": {}}
    if tokens.count(Database.dumps(context)) > limit:
        return {}, len(items)
    query = query.casefold().replace("_", " ")
    items.sort(key=lambda entry: (entry[1].replace("_", " ") not in query, entry[2].status != "pending", entry[1]))
    omitted = 0
    for instance, parameter, item in items:
        parent = context["instances"].setdefault(instance, {}) if instance else context["parameters"]
        parent[parameter] = item.context()
        if tokens.count(Database.dumps(context)) > limit:
            del parent[parameter]
            if instance and not parent:
                del context["instances"][instance]
            omitted += 1
    return context, omitted


class ConfigService:
    def __init__(self, db: Database = database, workspaces: WorkspaceService = workspace_service):
        self.db = db
        self.workspaces = workspaces

    def get_config(
        self, project_id: str, module_name: str | None = None, *, workspace_id: str | None = None,
    ) -> ProjectDesignConfig:
        workspaces = self._workspaces(project_id, workspace_id)
        _, config = self._read(project_id, workspaces)
        return config if module_name is None else config.scope(module_name)

    def get_pending_items(self, project_id: str, module_name: str) -> list[DesignConfigItem]:
        config = self.get_config(project_id, module_name)
        module = config.modules.get(normalize_module(module_name), ModuleDesignConfig())
        return [item for _, _, item in module.items() if item.status == "pending"]

    def apply_updates(
        self, project_id: str, module_name: str, updates: list[ConfigUpdate], *,
        source: ConversationMessageView, baseline: ProjectDesignConfig, workspace_id: str | None = None,
    ) -> ProjectDesignConfig:
        name = normalize_module(module_name)
        if not updates:
            return self.get_config(project_id, name, workspace_id=workspace_id)
        if baseline.project_id != project_id or source.role != "user":
            raise ValueError("Invalid configuration source")
        edits: dict[tuple[str | None, str], ConfigUpdate] = {}
        for update in updates:
            if update.module != name or update.evidence.strip() not in source.content or not update.evidence.strip():
                raise ValueError("Config updates require current user evidence in the requested module")
            key = update.instance, update.parameter
            previous = edits.get(key)
            if previous and not same_config_value(previous.value, update.value):
                raise ValueError("Conflicting updates for one design parameter")
            edits[key] = update

        with self.db.transaction() as conn:
            stored_source = conn.execute(
                """SELECT 1 FROM conversation_messages m JOIN conversations c USING(session_id)
                WHERE m.message_id = ? AND m.role = 'user' AND m.content = ?
                  AND c.project_id = ? AND c.module_name = ?""",
                (source.message_id, source.content, project_id, module_name),
            ).fetchone()
            if not stored_source:
                raise ValueError("Configuration evidence must be a persisted request in this scope")
            workspaces = self._workspaces(project_id, workspace_id)
            target, config = self._read(project_id, workspaces)
            module = config.modules.setdefault(name, ModuleDesignConfig())
            old_module = baseline.modules.get(name, ModuleDesignConfig())
            changed = False
            for (instance, parameter), update in edits.items():
                prior = module.get(instance, parameter)
                if prior and same_config_value(prior.value, update.value):
                    continue
                expected = old_module.get(instance, parameter)
                if self._version(prior) != self._version(expected):
                    raise ConfigRevisionConflict(f"Configuration changed for {name}.{parameter}")
                fields = {
                    "value": update.value,
                    "source_message_id": source.message_id,
                    "source_evidence": update.evidence.strip(),
                    "revision": prior.revision + 1 if prior else 1,
                }
                if prior:
                    fields["config_item_id"] = prior.config_item_id
                    fields["history"] = [*prior.history, prior.previous_revision()]
                module.set(instance, parameter, DesignConfigItem(**fields))
                changed = True
            if changed and target is not None:
                config.revision += 1
                self._write(target, config)
                return config.scope(name)
            if not changed:
                return config.scope(name)

        # Register a Workspace before publishing its config. Recheck all revisions on
        # reentry so a concurrent request cannot publish an outdated patch.
        self._ensure_workspace(project_id)
        return self.apply_updates(
            project_id, module_name, updates, source=source, baseline=baseline, workspace_id=workspace_id,
        )

    def record_input(self, run_id: str, config: ProjectDesignConfig) -> None:
        self.db.execute(
            """INSERT INTO run_design_config (run_id, input_json) VALUES (?, ?)
            ON CONFLICT(run_id) DO NOTHING""",
            (run_id, config.model_dump_json()),
        )

    def run_config(self, run_id: str, *, final: bool = False) -> ProjectDesignConfig | None:
        row = self.db.fetch_one("SELECT input_json, final_json FROM run_design_config WHERE run_id = ?", (run_id,))
        if not row:
            return None
        return ProjectDesignConfig.model_validate_json((row["final_json"] if final else None) or row["input_json"])

    def complete_run(self, run_id: str, check: Callable[[dict, ProjectDesignConfig], ConfigProofs]) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT r.*, s.input_json FROM runs r JOIN run_design_config s USING(run_id)
                WHERE r.run_id = ? AND r.status = 'completed' AND s.final_json IS NULL""", (run_id,),
            ).fetchone()
            if row is None:
                return
            run = dict(row)
            pinned = ProjectDesignConfig.model_validate_json(run["input_json"])
            name = normalize_module(run["module_name"])
            if pinned.project_id != run["project_id"]:
                raise ValueError("Run configuration project mismatch")
            module = pinned.modules.get(name)
            if module and pinned.workspace_id:
                target, current = self._read(run["project_id"], self._workspaces(run["project_id"], pinned.workspace_id))
                if target is not None and current.workspace_id == pinned.workspace_id:
                    live_module = current.modules.get(name, ModuleDesignConfig())
                    eligible = [
                        (item, live) for instance, parameter, item in module.items()
                        if (live := live_module.get(instance, parameter)) is not None
                        and self._version(live) == self._version(item)
                        and same_config_value(live.value, item.value) and live.status != "verified"
                    ]
                    if eligible:
                        self._complete_items(target, current, eligible, check(run, pinned))
            conn.execute("UPDATE run_design_config SET final_json = ? WHERE run_id = ?", (pinned.model_dump_json(), run_id))

    def _complete_items(
        self, target: WorkspaceView, current: ProjectDesignConfig,
        eligible: list[tuple[DesignConfigItem, DesignConfigItem]], proofs: ConfigProofs,
    ) -> None:
        changed = False
        for pinned, live in eligible:
            proof = proofs.get(pinned.config_item_id)
            if not proof:
                continue
            status, refs = proof
            if refs and _STATUS_RANK[status] > _STATUS_RANK[pinned.status]:
                pinned.status, pinned.evidence_refs = status, refs
            if refs and _STATUS_RANK[status] > _STATUS_RANK[live.status]:
                live.status = status
                live.evidence_refs = list({ref.model_dump_json(): ref for ref in [*live.evidence_refs, *refs]}.values())
                changed = True
        if changed:
            self._write(target, current)

    def _workspaces(self, project_id: str, requested: str | None) -> list[WorkspaceView]:
        workspaces = self.workspaces.list(project_id)
        if requested:
            selected = next((item for item in workspaces if item.workspace_id == requested), None)
            if selected is None:
                raise ValueError("Project Workspace is unavailable")
            workspaces = [selected, *(item for item in workspaces if item.workspace_id != requested)]
        else:
            workspaces.sort(key=lambda item: (item.created_at, item.workspace_id))
        return workspaces

    def _read(self, project_id: str, workspaces: list[WorkspaceView]) -> tuple[WorkspaceView | None, ProjectDesignConfig]:
        found = []
        for workspace in workspaces:
            path = self.file_path(workspace)
            if path.exists():
                found.append((workspace, path))
        if len(found) > 1:
            raise ValueError("More than one design configuration file is registered for this project")
        if not found:
            default = next(iter(workspaces), None)
            return default, ProjectDesignConfig(project_id=project_id, workspace_id=default.workspace_id if default else None)
        workspace, source = found[0]
        config = ProjectDesignConfig.model_validate_json(source.read_bytes())
        if (config.project_id, config.workspace_id) != (project_id, workspace.workspace_id):
            raise ValueError("Design configuration file scope mismatch")
        return workspace, config

    def file_path(self, workspace: WorkspaceView) -> Path:
        root = Path(workspace.root_path).resolve()
        self.workspaces._assert_within_root(root)
        path = self.workspaces.resolve_path(workspace.workspace_id, DESIGN_CONFIG_PATH)
        if path != root / DESIGN_CONFIG_PATH:
            raise ValueError("Design configuration path cannot use symbolic links")
        return path

    def _write(self, workspace: WorkspaceView, config: ProjectDesignConfig) -> None:
        self.file_path(workspace)
        self.workspaces.write_design_config(workspace.workspace_id, config.model_dump_json(indent=2) + "\n")

    def _ensure_workspace(self, project_id: str) -> None:
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM workspaces WHERE project_id = ?", (project_id,)).fetchone():
                self.workspaces.create(WorkspaceCreateRequest(project_id=project_id), connection=conn)

    @staticmethod
    def _version(item: DesignConfigItem | None) -> tuple[str, int] | None:
        return (item.config_item_id, item.revision) if item else None


config_service = ConfigService()
