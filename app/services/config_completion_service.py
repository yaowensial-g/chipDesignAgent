"""Advance implementation status only from linked, current engineering evidence."""

import hashlib
import re
from dataclasses import dataclass

from app.domain.design_config import ConfigEvidence, ProjectDesignConfig, normalize_module, normalize_parameter
from app.services.config_service import ConfigProofs, ConfigService
from app.tools.adapters.verilator_adapter import SIMULATION_TOOLS
from app.tools.project_manifest import FrozenProject, read_project


_TOKEN_OR_COMMENT = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
_MODULE = re.compile(r"\bmodule\s+(?:automatic\s+|static\s+)?([A-Za-z_][\w$]*)\b(.*?)\bendmodule\b", re.S)
_PARAMETER = re.compile(
    r"\bparameter\s+(?:(?:int|integer|shortint|longint|logic|bit|reg|signed|unsigned)\s+)*"
    r"([A-Za-z_][\w$]*)\s*=\s*([+-]?\s*[0-9][\d_]*)\s*(?=[,;)])",
)


@dataclass(frozen=True)
class RTLProof:
    path: str
    text: str
    reference: ConfigEvidence
    snapshot_id: str | None = None


class ConfigCompletionService:
    def __init__(self, configs: ConfigService):
        self.configs = configs
        self.db = configs.db
        self.workspaces = configs.workspaces

    def complete(self, run_id: str) -> None:
        self.configs.complete_run(run_id, self._check)

    def _check(self, run: dict, pinned: ProjectDesignConfig) -> ConfigProofs:
        workspace_id = run["workspace_id"]
        workspace = self.workspaces.get(workspace_id) if workspace_id else None
        if not workspace or workspace.project_id != run["project_id"]:
            return {}
        name = normalize_module(run["module_name"])
        declared = pinned.modules[name].parameters
        if not any(type(item.value) is int for item in declared.values()):
            return {}
        sources, projects = self._sources(run)
        passed = self._passed_cases(run, projects)
        proofs: ConfigProofs = {}
        for source in sources:
            for parameter, value in _declared_parameters(source.text, name).items():
                item = declared.get(parameter)
                if item is None or type(item.value) is not int or item.value != value:
                    continue
                proof = [source.reference]
                for testcase, result_ref in passed.get(source.snapshot_id, []):
                    case = projects[source.snapshot_id].manifest.testcases[testcase]
                    if source.path in case.depends_on and any(
                        (target.module, target.parameter, target.value) == (name, parameter, value)
                        for target in case.config_checks
                    ):
                        proof.append(result_ref)
                item_id = item.config_item_id
                if len(proof) > 1:
                    proofs[item_id] = ("verified", proof)
                else:
                    proofs.setdefault(item_id, ("implemented", proof))
        return proofs

    def _sources(self, run: dict) -> tuple[list[RTLProof], dict[str, FrozenProject]]:
        sources: list[RTLProof] = []
        projects: dict[str, FrozenProject] = {}
        if run["patch_id"]:
            patch = self.db.fetch_one(
                """SELECT patch_id, target_path, after_content FROM patches
                WHERE patch_id = ? AND workspace_id = ? AND status = 'applied'""",
                (run["patch_id"], run["workspace_id"]),
            )
            if patch:
                path = patch["target_path"]
                text = self._read_rtl(run, path)
                if text is not None and text.replace("\r\n", "\n") == patch["after_content"].replace("\r\n", "\n"):
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    sources.append(RTLProof(path, text, ConfigEvidence(
                        kind="patch_approval", source_id=patch["patch_id"], location=path, content_hash=digest,
                    )))
        if run["engine_version"] != "hierarchical":
            return sources, projects
        rows = self.db.fetch_all(
            """SELECT a.artifact_id, a.content_hash, a.artifact_json, b.content
            FROM orchestration_artifacts a JOIN orchestration_tasks t USING(task_id)
            JOIN orchestration_blobs b ON b.content_hash = a.content_hash
            WHERE t.run_id = ? AND a.status = 'verified'
              AND json_extract(a.artifact_json, '$.artifact_type') IN ('rtl_snapshot', 'project_manifest')
              AND json_extract(a.artifact_json, '$.source_generation') = json_extract(t.state_json, '$.generation')
              AND json_extract(t.state_json, '$.status') = 'completed'
            ORDER BY a.rowid DESC""",
            (run["run_id"],),
        )
        for row in rows:
            data = row["content"]
            if hashlib.sha256(data).hexdigest() != row["content_hash"]:
                continue
            artifact = self.db.loads(row["artifact_json"], {})
            if artifact["artifact_type"] == "rtl_snapshot":
                path = artifact["metadata"]["relative_path"]
                text = self._read_rtl(run, path)
                if text is not None and text.encode() == data:
                    sources.append(self._artifact_source(row, path, text))
                continue
            frozen = FrozenProject.model_validate_json(data)
            try:
                current = read_project(self.workspaces, run["workspace_id"], run["project_id"], frozen.manifest_path)
            except (OSError, ValueError, KeyError):
                continue
            if current.input_hash != frozen.input_hash or current != frozen:
                continue
            projects[row["artifact_id"]] = frozen
            for path in frozen.manifest.sources:
                sources.append(self._artifact_source(row, path, frozen.files[path]))
        return sources, projects

    def _read_rtl(self, run: dict, path: str) -> str | None:
        try:
            return self.workspaces.read_rtl_source(run["workspace_id"], run["project_id"], path).decode("utf-8")
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def _artifact_source(row: dict, path: str, text: str) -> RTLProof:
        ref = ConfigEvidence(kind="artifact", source_id=row["artifact_id"], location=path, content_hash=row["content_hash"])
        return RTLProof(path, text, ref, row["artifact_id"])

    def _passed_cases(self, run: dict, projects: dict) -> dict[str, list[tuple[str, ConfigEvidence]]]:
        if not projects:
            return {}
        rows = self.db.fetch_all(
            """SELECT l.tool_run_id, l.target_json, u.tool_name, p.result_json
            FROM orchestration_tool_runs l JOIN orchestration_actions a ON a.action_id = l.action_id
            JOIN orchestration_tasks t ON t.task_id = a.task_id
            JOIN tool_runs u ON u.run_id = l.tool_run_id
            JOIN eda_tool_results p ON p.tool_run_id = u.run_id
            WHERE t.run_id = ? AND u.workspace_id = ? AND u.status = 'completed' AND u.exit_code = 0
              AND a.status = 'done' AND a.generation = json_extract(t.state_json, '$.generation')
              AND json_extract(t.state_json, '$.status') = 'completed'""",
            (run["run_id"], run["workspace_id"]),
        )
        passed: dict[str, list[tuple[str, ConfigEvidence]]] = {}
        for row in rows:
            if row["tool_name"] not in SIMULATION_TOOLS:
                continue
            target = self.db.loads(row["target_json"], {})
            snapshot_id = target.get("snapshot_ref")
            project = projects.get(snapshot_id)
            result = self.db.loads(row["result_json"], {})
            if (project is None or result.get("status") != "PASS" or result.get("exit_code") != 0
                or result.get("tool_name") != row["tool_name"]
                or result.get("input_hash") != project.input_hash or target.get("content_hash") != project.input_hash):
                continue
            selected = target.get("arguments", {}).get("testcases", [])
            for case in result.get("metrics", {}).get("testcases", []):
                name = case.get("testcase")
                if name not in selected or name not in project.manifest.testcases:
                    continue
                if case.get("status") == "PASS" and case.get("exit_code") == 0:
                    passed.setdefault(snapshot_id, []).append((name, ConfigEvidence(
                        kind="tool_run", source_id=row["tool_run_id"], location=project.manifest_path,
                        content_hash=project.input_hash, testcase=name,
                    )))
        return passed


def _declared_parameters(text: str, module: str) -> dict[str, int]:
    """Recognize literal integer defaults in the module header or declaration region."""
    text = _TOKEN_OR_COMMENT.sub(" ", text)
    # Conditional defaults, macros and generated scopes need real elaboration.
    if "`" in text:
        return {}
    modules = [body for name, body in _MODULE.findall(text) if name == module]
    if len(modules) != 1:
        return {}
    declarations = re.split(r"\b(?:generate|function|task|class|if|for|always|always_comb|always_ff|initial)\b", modules[0], maxsplit=1)[0]
    parameters: dict[str, int] = {}
    ambiguous = set()
    for name, value in _PARAMETER.findall(declarations):
        name = normalize_parameter(name)
        if name in parameters:
            ambiguous.add(name)
        parameters[name] = int(re.sub(r"[\s_]", "", value))
    return {name: value for name, value in parameters.items() if name not in ambiguous}
