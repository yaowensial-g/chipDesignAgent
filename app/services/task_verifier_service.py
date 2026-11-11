"""Deterministic acceptance checks for the read-only task engine."""

from hashlib import sha256
import json

from app.domain.enums import ToolRunStatus

from app.domain.orchestration import (
    ArtifactPayload, Criterion, EvidenceRef, LintResult, RTLDesignAnalysis, RTLSourceSnapshot,
    RTLVerificationAssessment, TaskContract, VerificationResult,
)
from app.services.tool_run_service import ToolRunService
from app.tools.eda_catalog import EDA_INSPECT
from app.tools.project_manifest import FrozenProject


class TaskVerifierService:
    def __init__(self, tools: ToolRunService | None = None) -> None:
        self.tools = tools

    def verify(
        self, contract: TaskContract, submitted: tuple[ArtifactPayload, ...],
        inputs: tuple[ArtifactPayload, ...], criteria: tuple[Criterion, ...],
    ) -> VerificationResult:
        outputs = {item.key: item for item in contract.outputs}
        candidates = {item.artifact.output_key: item for item in submitted}
        failed = []
        snapshots: dict[str, RTLSourceSnapshot] = {}

        for item in (*inputs, *submitted):
            if item.artifact.artifact_type == "rtl_snapshot":
                try:
                    snapshot = self._snapshot(item)
                    snapshots[snapshot.content_ref] = snapshot
                except (ValueError, KeyError):
                    failed.append("invalid_rtl_snapshot")

        if len(submitted) != len(candidates):
            failed.append("duplicate_output")
        for key, spec in outputs.items():
            item = candidates.get(key)
            if item is None:
                if spec.required:
                    failed.append(f"missing:{key}"[:120])
                continue
            artifact = item.artifact
            try:
                if (
                    artifact.status != "staged" or artifact.source_task_id != contract.task_id
                    or artifact.artifact_type != spec.artifact_type or artifact.content_ref.media_type != spec.media_type
                ):
                    raise ValueError("Invalid candidate scope")
                self._check_content(item)
                self._check_type(item, snapshots, inputs)
            except (ValueError, KeyError):
                failed.append(f"invalid:{key}"[:120])
        if candidates.keys() - outputs.keys():
            failed.append("unknown_output")
        for criterion in criteria:
            if criterion.rule_id != "required_output" or not set(criterion.output_keys) <= candidates.keys():
                failed.append(criterion.criterion_id)
        return VerificationResult(
            passed=not failed, failed_criteria=tuple(dict.fromkeys(failed)), repairable=bool(failed),
            suggestions=("补齐交付件，检查内容哈希、输出格式及 RTL 证据引用。",) if failed else (),
            evidence=tuple(EvidenceRef(kind="artifact", ref_id=item.artifact.artifact_id) for item in submitted),
        )

    @staticmethod
    def _check_content(item: ArtifactPayload) -> None:
        digest = sha256(item.content).hexdigest()
        ref = item.artifact.content_ref
        if not item.content or ref.byte_size != len(item.content) or digest != ref.object_key or digest != item.artifact.content_hash:
            raise ValueError("Content does not match recorded reference")

    def confirms_invalidation(
        self, item: ArtifactPayload, producer_inputs: tuple[ArtifactPayload, ...],
        sibling_outputs: tuple[ArtifactPayload, ...],
    ) -> bool:
        """Independently recheck the original evidence and deterministic acceptance rules."""
        try:
            self._check_content(item)
        except ValueError:
            return True
        kind = item.artifact.artifact_type
        if kind == "rtl_snapshot":
            try:
                self._snapshot(item)
            except (ValueError, KeyError):
                return True
            return False
        try:
            snapshots = {
                source.artifact.artifact_id: self._snapshot(source)
                for source in (*producer_inputs, *sibling_outputs)
                if source.artifact.artifact_type == "rtl_snapshot" and kind in {"design_analysis", "verification_assessment", "lint_result"}
            }
        except (ValueError, KeyError):
            return False
        try:
            self._check_type(item, snapshots, producer_inputs)
        except ValueError:
            return True
        except KeyError:
            return False
        return False

    def _snapshot(self, item: ArtifactPayload) -> RTLSourceSnapshot:
        self._check_content(item)
        artifact = item.artifact
        return RTLSourceSnapshot(
            relative_path=artifact.metadata["relative_path"], content_ref=artifact.artifact_id,
            byte_size=len(item.content), content_hash=artifact.content_hash,
            line_count=len(item.content.decode("utf-8").splitlines()),
        )

    def _check_type(
        self, item: ArtifactPayload, snapshots: dict[str, RTLSourceSnapshot], inputs: tuple[ArtifactPayload, ...],
    ) -> None:
        artifact_type = item.artifact.artifact_type
        if artifact_type == "rtl_snapshot":
            if item.artifact.artifact_id not in snapshots:
                raise ValueError("Invalid RTL snapshot")
            return
        if artifact_type == "report":
            report = item.content.decode("utf-8")
            if not report.strip() or any(
                f"[artifact:{source.artifact.artifact_id}]" not in report
                for source in inputs if source.artifact.artifact_type in {
                    "design_analysis", "verification_assessment", "lint_result", "project_manifest", "eda_result", *EDA_INSPECT,
                }
            ):
                raise ValueError("Report is empty or misses required citations")
            return
        if artifact_type == "lint_result":
            self._check_lint(item, snapshots)
            return
        if artifact_type == "project_manifest":
            project = FrozenProject.model_validate_json(item.content)
            if not project.files or any(
                sha256(text.encode()).hexdigest() != project.hashes.get(path) for path, text in project.files.items()
            ):
                raise ValueError("Invalid frozen project contents")
            return
        if artifact_type == "eda_result":
            self._check_eda(item, inputs)
            return
        if artifact_type in EDA_INSPECT:
            if not isinstance(json.loads(item.content), dict) or not any(
                ref.ref_id == source.artifact.artifact_id
                for ref in item.artifact.evidence for source in inputs
            ):
                raise ValueError("Missing bound project evidence")
            return
        if artifact_type == "design_analysis":
            findings = RTLDesignAnalysis.model_validate_json(item.content).findings
        elif artifact_type == "verification_assessment":
            review = RTLVerificationAssessment.model_validate_json(item.content)
            findings = (*review.impacts, *review.recommended_checks)
        else:
            raise ValueError("Unsupported artifact type")
        for finding in findings:
            for evidence in finding.evidence:
                if not evidence.matches(snapshots[evidence.snapshot_ref]):
                    raise ValueError("Invalid source evidence")

    def _check_lint(self, item: ArtifactPayload, snapshots: dict[str, RTLSourceSnapshot]) -> None:
        if self.tools is None:
            raise ValueError("ToolRun evidence cannot be resolved")
        proof = LintResult.model_validate_json(item.content)
        link = self.tools.frozen_tool_link(item.artifact.source_action_id)
        tool = self.tools.get(proof.tool_run_id)
        snapshot = snapshots.get(proof.snapshot_ref)
        if link is None or tool is None or snapshot is None:
            raise ValueError("Incomplete Lint provenance")
        target = json.loads(link["target_json"])
        if (
            link["tool_run_id"] != proof.tool_run_id or tool.tool_name != "verilator_lint"
            or tool.status != ToolRunStatus.COMPLETED or tool.exit_code != 0 or proof.exit_code != 0
            or proof.snapshot_ref != target["snapshot_ref"] or proof.content_hash != target["content_hash"]
            or proof.content_hash != snapshot.content_hash or proof.top_module != target["top_module"]
            or proof.version != target["version"]
            or tool.arguments != {"files": [snapshot.relative_path], "top_module": proof.top_module}
            or not tool.command or tool.command[0] != target["executable"] or "--lint-only" not in tool.command
            or proof.stdout_hash != sha256(tool.stdout.encode()).hexdigest()
            or proof.stderr_hash != sha256(tool.stderr.encode()).hexdigest()
        ):
            raise ValueError("Real Lint evidence failed verification")

    def _check_eda(self, item: ArtifactPayload, inputs: tuple[ArtifactPayload, ...]) -> None:
        proof = json.loads(item.content)
        link = self.tools.frozen_tool_link(item.artifact.source_action_id) if self.tools else None
        project = next((source for source in inputs if source.artifact.artifact_type == "project_manifest"), None)
        if link is None or project is None or proof.get("tool_run_id") != link["tool_run_id"]:
            raise ValueError("Missing approved EDA provenance")
        target = json.loads(link["target_json"])
        tool = self.tools.get(link["tool_run_id"])
        result = self.tools.eda_result(link["tool_run_id"])
        expected = {"PASS": ToolRunStatus.COMPLETED, "FAIL": ToolRunStatus.FAILED, "ERROR": ToolRunStatus.FAILED,
                    "TIMEOUT": ToolRunStatus.TIMED_OUT}
        if (
            tool is None or result is None or result.model_dump(mode="json") != proof.get("result")
            or expected[result.status] != tool.status or tool.tool_name != result.tool_name
            or tool.arguments != target["arguments"]
            or project.artifact.artifact_id != target["snapshot_ref"]
            or result.input_hash != FrozenProject.model_validate_json(project.content).input_hash
            or result.input_hash != target["content_hash"] or result.tool_version != target["version"]
            or (result.status == "PASS" and (result.exit_code != 0 or not result.commands))
        ):
            raise ValueError("EDA receipt does not match the actual ToolRun")
        for artifact in result.artifacts:
            raw = self.tools.eda_artifact(link["tool_run_id"], artifact["name"])
            if sha256(raw).hexdigest() != artifact["sha256"]:
                raise ValueError("EDA artifact digest mismatch")
