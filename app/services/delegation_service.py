from __future__ import annotations

import asyncio
from typing import Any, Callable
from uuid import uuid4

from app.agents.runtime import runtime
from app.domain.enums import AgentRole, DelegationStatus
from app.domain.models import (
    DelegationArtifactRef,
    DelegationRequest,
    DelegationResult,
)
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


MAX_DELEGATIONS_PER_RUN = 3
DELEGATION_TIMEOUT_SECONDS = 90
ALLOWED_EXPERTS = {
    AgentRole.DESIGN: {AgentRole.VERIFICATION, AgentRole.ARCHITECTURE},
    AgentRole.VERIFICATION: {AgentRole.DESIGN, AgentRole.ARCHITECTURE},
}


class DelegationService:
    """Runs bounded expert tasks while the primary Agent keeps conversation control."""

    def __init__(self, db: Database = database) -> None:
        self.db = db

    async def delegate(
        self,
        *,
        parent_run_id: str,
        from_agent: AgentRole,
        to_agent: AgentRole,
        task_type: str,
        instruction: str,
        request_payload: dict[str, Any],
        expected_output: str = "结构化专业分析与建议",
    ) -> DelegationResult:
        if to_agent not in ALLOWED_EXPERTS.get(from_agent, set()):
            raise PermissionError(
                f"{from_agent.value} cannot delegate to {to_agent.value}"
            )
        count = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM delegation_tasks WHERE parent_run_id = ?",
            (parent_run_id,),
        )
        if int(count["count"] or 0) >= MAX_DELEGATIONS_PER_RUN:
            raise ValueError("Delegation limit reached for this Run")

        artifacts = [
            DelegationArtifactRef(path=str(item.get("name", "document")), version=str(item.get("version", "")))
            for item in request_payload.get("documents", [])
        ]
        delegation = DelegationRequest(
            parent_run_id=parent_run_id,
            from_agent=from_agent,
            to_agent=to_agent,
            task_type=task_type,
            instruction=instruction,
            artifacts=artifacts,
            constraints={"read_only": True, "allow_delegation": False},
            expected_output=expected_output,
        )
        task_id = str(uuid4())
        now = utc_now()
        self.db.execute(
            """INSERT INTO delegation_tasks (
            task_id, parent_run_id, from_agent, to_agent, task_type, instruction,
            artifacts_json, constraints_json, expected_output, status, result_json,
            error, requires_human_intervention, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, 0, ?, ?)""",
            (
                task_id,
                parent_run_id,
                from_agent.value,
                to_agent.value,
                delegation.task_type,
                delegation.instruction,
                self.db.dumps([item.model_dump(mode="json") for item in artifacts]),
                self.db.dumps(delegation.constraints),
                delegation.expected_output,
                DelegationStatus.QUEUED.value,
                now,
                now,
            ),
        )
        self._set_status(task_id, DelegationStatus.RUNNING)

        scoped = self._scoped_context(request_payload, delegation)
        try:
            analysis = await asyncio.wait_for(
                asyncio.to_thread(self._expert_callable(to_agent), scoped),
                timeout=DELEGATION_TIMEOUT_SECONDS,
            )
            conclusion = str(analysis.get("summary", "专家分析已完成。"))
            evidence = self._evidence(analysis, artifacts)
            result_payload = {
                "conclusion": conclusion,
                "evidence": evidence,
                "artifacts": [item.model_dump(mode="json") for item in artifacts],
                "recommendations": [conclusion],
            }
            self.db.execute(
                """UPDATE delegation_tasks SET status = ?, result_json = ?, updated_at = ?
                WHERE task_id = ?""",
                (
                    DelegationStatus.COMPLETED.value,
                    self.db.dumps(result_payload),
                    utc_now(),
                    task_id,
                ),
            )
        except TimeoutError:
            self._fail(task_id, DelegationStatus.TIMED_OUT, "Expert delegation timed out")
        except Exception as exc:
            self._fail(task_id, DelegationStatus.FAILED, str(exc)[:1000])
        return self.get(task_id)

    def get(self, task_id: str) -> DelegationResult:
        row = self.db.fetch_one(
            "SELECT * FROM delegation_tasks WHERE task_id = ?", (task_id,)
        )
        if not row:
            raise KeyError(task_id)
        payload = self.db.loads(row["result_json"], {})
        return DelegationResult(
            task_id=row["task_id"],
            parent_run_id=row["parent_run_id"],
            from_agent=row["from_agent"],
            to_agent=row["to_agent"],
            status=row["status"],
            conclusion=str(payload.get("conclusion", "")),
            evidence=[str(item) for item in payload.get("evidence", [])],
            artifacts=[DelegationArtifactRef(**item) for item in payload.get("artifacts", [])],
            recommendations=[str(item) for item in payload.get("recommendations", [])],
            error=row["error"],
            requires_human_intervention=bool(row["requires_human_intervention"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_for_run(self, run_id: str) -> list[DelegationResult]:
        rows = self.db.fetch_all(
            "SELECT task_id FROM delegation_tasks WHERE parent_run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [self.get(row["task_id"]) for row in rows]

    @staticmethod
    def _scoped_context(
        request_payload: dict[str, Any], delegation: DelegationRequest
    ) -> dict[str, Any]:
        return {
            "project_id": request_payload["project_id"],
            "module_name": request_payload["module_name"],
            "message": delegation.instruction,
            "instruction": delegation.instruction,
            "documents": request_payload.get("documents", []),
            "history": [],
            "workspace_id": request_payload.get("workspace_id"),
            "delegation": delegation.model_dump(mode="json"),
        }

    @staticmethod
    def _expert_callable(role: AgentRole) -> Callable[[dict[str, Any]], dict[str, Any]]:
        mapping = {
            AgentRole.ARCHITECTURE: runtime.architecture_analysis,
            AgentRole.DESIGN: runtime.design_analysis,
            AgentRole.VERIFICATION: runtime.verification_analysis,
        }
        return mapping[role]

    @staticmethod
    def _evidence(
        analysis: dict[str, Any], artifacts: list[DelegationArtifactRef]
    ) -> list[str]:
        evidence = [f"{item.path}@{item.version}" for item in artifacts]
        for source in analysis.get("sources", []):
            if isinstance(source, dict):
                evidence.append(
                    f"{source.get('document', 'document')}@{source.get('version', '')}"
                )
        return list(dict.fromkeys(evidence))

    def _set_status(self, task_id: str, status: DelegationStatus) -> None:
        self.db.execute(
            "UPDATE delegation_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status.value, utc_now(), task_id),
        )

    def _fail(self, task_id: str, status: DelegationStatus, error: str) -> None:
        self.db.execute(
            """UPDATE delegation_tasks SET status = ?, error = ?,
            requires_human_intervention = 1, updated_at = ? WHERE task_id = ?""",
            (status.value, error, utc_now(), task_id),
        )


delegation_service = DelegationService()
