"""Transactional checkpoints for the hierarchical Run engine."""

import hashlib
import json
from datetime import datetime, timezone
from sqlite3 import Connection
from uuid import uuid4

from app.domain.enums import AgentRole
from app.domain.models import ChatRequest, RunResumeRequest, ToolSelection
from app.domain.orchestration import (
    ActionApproval, Artifact, ArtifactPayload, ContentRef, Observation, ReflectionRecord, TaskActionResult,
    TaskContract, TaskOutputRef, TaskPlan, TaskState, VerificationResult,
)
from app.services.session_service import SessionService
from app.services.workspace_service import utc_now
from app.storage.database import Database


def now() -> datetime:
    return datetime.now(timezone.utc)


class OrchestrationRepository:
    def __init__(self, db: Database, sessions: SessionService) -> None:
        self.db = db
        self.sessions = sessions

    def create(self, session_id: str, role: AgentRole, request: ChatRequest) -> str:
        run_id = str(uuid4())
        timestamp = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO runs (
                   run_id, session_id, project_id, module_name, workspace_id, run_type, status,
                   user_instruction, route_reason, primary_agent, engine_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'orchestration', 'planned', ?, ?, ?, 'hierarchical', ?, ?)""",
                (run_id, session_id, request.project_id, request.module_name, request.workspace_id,
                 request.message, "分层任务编排", role.value, timestamp, timestamp),
            )
            message = self.sessions.add_message(
                session_id, "user", request.message, agent_role=role.value, run_id=run_id, transaction=conn,
            )
            conn.execute(
                """INSERT INTO orchestration_runs(run_id, root_message_id, turn_id, request_json, snapshot)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, message.message_id, message.turn_id,
                 request.model_dump_json(exclude={"documents", "history", "ds_markdown", "tool_arguments", "user_approved"}),
                 self.db.dumps({"uploads": [item.model_dump(mode="json") for item in request.documents]}).encode()),
            )
            conn.execute("UPDATE conversations SET active_run_id = ? WHERE session_id = ?", (run_id, session_id))
            self.event(conn, run_id, "run_created", "已创建分层编排 Run。", role.value)
        return run_id

    def checkpoint(self, run_id: str) -> dict:
        row = self.db.fetch_one("SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise KeyError(run_id)
        return row

    def recoverable(self) -> list[str]:
        return [
            row["run_id"] for row in self.db.fetch_all(
                """SELECT h.run_id FROM orchestration_runs h JOIN runs r ON r.run_id = h.run_id
                   WHERE r.status IN ('planned', 'running') ORDER BY r.created_at"""
            )
        ]

    def external_runs(self) -> list[str]:
        return [row["run_id"] for row in self.db.fetch_all(
            """SELECT DISTINCT t.run_id FROM orchestration_tool_runs link
               JOIN orchestration_actions a ON a.action_id=link.action_id
               JOIN orchestration_tasks t ON t.task_id=a.task_id
               JOIN runs r ON r.run_id=t.run_id WHERE a.status='dispatching' AND r.status='running'""",
        )]

    def mark_external(self, state: TaskState, tool_run_id: str) -> None:
        if state.phase == "awaiting_external":
            return
        with self.db.transaction() as conn:
            self.save_state(conn, state.model_copy(update={"phase": "awaiting_external"}))
            self.update_run(conn, state.run_id, "running", result={"lint_tool_run_id": tool_run_id})
            self.event(conn, state.run_id, "tool_started", f"Verilator ToolRun {tool_run_id}", task_id=state.task_id)

    def latest_tool_run_id(self, run_id: str) -> str | None:
        row = self.db.fetch_one(
            """SELECT link.tool_run_id FROM orchestration_tool_runs link
               JOIN orchestration_actions a ON a.action_id=link.action_id
               JOIN orchestration_tasks t ON t.task_id=a.task_id WHERE t.run_id=?
               ORDER BY a.decision_sequence DESC LIMIT 1""", (run_id,),
        )
        return row["tool_run_id"] if row else None

    def approved_action(self, action_id: str) -> ActionApproval | None:
        row = self.db.fetch_one(
            """SELECT request_json, decision_json FROM orchestration_waits
               WHERE action_id=? AND status='resolved' ORDER BY rowid DESC LIMIT 1""", (action_id,),
        )
        if row and json.loads(row["decision_json"])["action"] == "approve":
            return ActionApproval.model_validate(json.loads(row["request_json"])["approval"])
        return None

    def save_snapshot(self, run_id: str, context_id: str, payload: dict) -> None:
        self.db.execute(
            "UPDATE orchestration_runs SET snapshot = ?, context_id = ? WHERE run_id = ?",
            (self.db.dumps(payload).encode(), context_id, run_id),
        )

    def save_plan(self, plan: TaskPlan) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE orchestration_runs SET plan_json = ? WHERE run_id = ?", (plan.model_dump_json(), plan.run_id))
            for task in plan.tasks:
                state = TaskState(run_id=plan.run_id, task_id=task.task_id, generation=1, status="pending")
                conn.execute("INSERT INTO orchestration_tasks VALUES (?, ?, ?)", (task.task_id, plan.run_id, state.model_dump_json()))
            self.update_run(conn, plan.run_id, "running")
            self.event(conn, plan.run_id, "plan_created", f"已建立 {len(plan.tasks)} 个任务。")
            self.event(conn, plan.run_id, "plan_validated", "任务依赖与能力范围已通过校验。")

    def states(self, run_id: str) -> dict[str, TaskState]:
        rows = self.db.fetch_all("SELECT state_json FROM orchestration_tasks WHERE run_id = ?", (run_id,))
        states = [TaskState.model_validate_json(row["state_json"]) for row in rows]
        return {state.task_id: state for state in states}

    @staticmethod
    def save_state(conn: Connection, state: TaskState) -> None:
        conn.execute("UPDATE orchestration_tasks SET state_json = ? WHERE task_id = ?", (state.model_dump_json(), state.task_id))

    def claim(self, state: TaskState, contract: TaskContract) -> TaskState:
        with self.db.transaction() as conn:
            for spec in contract.inputs:
                row = None
                if isinstance(spec.source, TaskOutputRef):
                    row = conn.execute(
                        """SELECT artifact_id FROM orchestration_artifacts
                           WHERE task_id = ? AND output_key = ? AND status = 'verified'
                           ORDER BY version DESC LIMIT 1""",
                        (spec.source.task_id, spec.source.output_key),
                    ).fetchone()
                if row is None and spec.required:
                    raise ValueError(f"Required verified input is unavailable: {spec.key}")
                conn.execute(
                    "INSERT INTO orchestration_bindings VALUES (?, ?, ?)",
                    (state.task_id, spec.key, row["artifact_id"] if row else None),
                )
            claimed = conn.execute(
                "UPDATE orchestration_runs SET active_task_id = ? WHERE run_id = ? AND active_task_id IS NULL",
                (state.task_id, state.run_id),
            ).rowcount
            if claimed != 1:
                raise ValueError("Run already has an active task")
            state = state.model_copy(update={"status": "running", "phase": "selecting", "started_at": now()})
            self.save_state(conn, state)
            self.event(conn, state.run_id, "task_started", contract.title, contract.assigned_agent.value, state.task_id)
        return state

    def reserve(self, state: TaskState) -> tuple[TaskState, str]:
        action_id = str(uuid4())
        state = state.model_copy(update={"current_iteration": state.current_iteration + 1})
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO orchestration_actions(action_id, task_id, decision_sequence, status)
                   VALUES (?, ?, ?, 'selecting')""",
                (action_id, state.task_id, state.current_iteration),
            )
            self.save_state(conn, state)
        return state, action_id

    def last_action(self, task_id: str) -> dict | None:
        return self.db.fetch_one(
            """SELECT * FROM orchestration_actions WHERE task_id = ?
               ORDER BY decision_sequence DESC LIMIT 1""", (task_id,),
        )

    def select(
        self, state: TaskState, selection: ToolSelection, shortlist: list[str], reflection: ReflectionRecord,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE orchestration_actions SET status='selected', selection_json=?, shortlist_json=?, reflection_json=?
                   WHERE action_id=? AND status='selecting'""",
                (selection.model_dump_json(), self.db.dumps(shortlist), reflection.model_dump_json(), reflection.action_id),
            )
            self.event(conn, state.run_id, "action_selected", selection.name, task_id=state.task_id)
            self.event(conn, state.run_id, "reflection_recorded", reflection.result.progress_summary, task_id=state.task_id)

    def dispatch(self, state: TaskState, action_id: str, count: bool) -> TaskState:
        state = state.model_copy(update={"action_count": state.action_count + int(count), "phase": "dispatching"})
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE orchestration_actions SET status='dispatching' WHERE action_id=? AND status='selected'",
                (action_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("Action has already been dispatched")
            self.save_state(conn, state)
        return state

    def observations(self, task_id: str) -> tuple[Observation, ...]:
        return tuple(Observation.model_validate_json(row["observation_json"]) for row in self.db.fetch_all(
            """SELECT o.observation_json FROM orchestration_observations o JOIN orchestration_actions a
               ON a.action_id=o.action_id WHERE a.task_id=? ORDER BY a.decision_sequence""", (task_id,),
        ))

    def reflections(self, task_id: str) -> list[ReflectionRecord]:
        return [ReflectionRecord.model_validate_json(row["reflection_json"]) for row in self.db.fetch_all(
            """SELECT reflection_json FROM orchestration_actions WHERE task_id=? AND reflection_json IS NOT NULL
               ORDER BY decision_sequence""", (task_id,),
        )]

    def bound_inputs(self, task_id: str) -> tuple[ArtifactPayload, ...]:
        return self._artifacts(
            """JOIN orchestration_bindings i ON i.artifact_id=a.artifact_id
               WHERE i.task_id=? AND a.status='verified' ORDER BY i.input_key""", (task_id,),
        )

    def local_outputs(self, task_id: str, status: str) -> tuple[ArtifactPayload, ...]:
        return self._artifacts(
            """WHERE a.task_id=? AND a.status=? ORDER BY a.output_key, a.version""", (task_id, status),
        )

    def _artifacts(self, suffix: str, args: tuple) -> tuple[ArtifactPayload, ...]:
        rows = self.db.fetch_all(
            """SELECT a.artifact_json, a.status, b.content FROM orchestration_artifacts a
               JOIN orchestration_blobs b ON a.content_hash=b.content_hash """ + suffix, args,
        )
        return tuple(
            ArtifactPayload(
                artifact=Artifact.model_validate_json(row["artifact_json"]).model_copy(update={"status": row["status"]}),
                content=row["content"],
            )
            for row in rows
        )

    def version(self, task_id: str, key: str) -> int:
        row = self.db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) AS version FROM orchestration_artifacts WHERE task_id=? AND output_key=?",
            (task_id, key),
        )
        return int(row["version"]) + 1

    def record_outcome(
        self, state: TaskState, observation: Observation, result: dict,
        staged: list[ArtifactPayload], *, verify: bool = False, pending: dict | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            action_status = conn.execute(
                "SELECT status FROM orchestration_actions WHERE action_id=?", (observation.action_id,),
            ).fetchone()["status"]
            for item in staged:
                artifact = item.artifact
                conn.execute("INSERT OR IGNORE INTO orchestration_blobs VALUES (?, ?)", (artifact.content_hash, item.content))
                conn.execute(
                    "INSERT INTO orchestration_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (artifact.artifact_id, state.task_id, observation.action_id, artifact.output_key,
                     artifact.version, artifact.status, artifact.content_hash, artifact.model_dump_json()),
                )
            approval_wait = pending is not None and pending["kind"] == "tool_approval"
            conn.execute(
                "UPDATE orchestration_actions SET status=?, result_json=? WHERE action_id=?",
                ("waiting_approval" if approval_wait else "done", self.db.dumps(result), observation.action_id),
            )
            if not approval_wait:
                conn.execute(
                    "INSERT INTO orchestration_observations VALUES (?, ?, ?)",
                    (observation.observation_id, observation.action_id, observation.model_dump_json()),
                )
            if pending is not None:
                status = "waiting_for_approval" if pending["kind"] == "tool_approval" else "waiting_for_user"
                pending = self.open_wait(conn, state.run_id, pending, state.task_id, observation.action_id, status)
                self.post_response(conn, state.run_id, pending["message"])
                state = state.model_copy(update={"status": status, "phase": None, "wait_id": pending["wait_id"]})
            else:
                state = state.model_copy(update={"phase": "verifying" if verify else "selecting"})
            self.save_state(conn, state)
            if action_status == "dispatching" and observation.tool_name not in {"submit_task_output", "ask_user", "report_task_failure"}:
                self.event(conn, state.run_id, "tool_executed", observation.tool_name, task_id=state.task_id)
            if not approval_wait:
                self.event(conn, state.run_id, "observation_recorded", observation.summary, task_id=state.task_id)

    def record_verification(
        self, state: TaskState, action_id: str, result: VerificationResult, artifact_ids: tuple[str, ...],
        *, global_result: VerificationResult | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO orchestration_verifications VALUES (?, ?, 'task', ?, ?, ?)",
                (str(uuid4()), action_id, self.db.dumps(artifact_ids), result.model_dump_json(), utc_now()),
            )
            if global_result is not None:
                conn.execute(
                    "INSERT INTO orchestration_verifications VALUES (?, ?, 'run', ?, ?, ?)",
                    (str(uuid4()), action_id, self.db.dumps(artifact_ids), global_result.model_dump_json(), utc_now()),
                )
            passed = result.passed and (global_result is None or global_result.passed)
            if passed:
                for artifact_id in artifact_ids:
                    conn.execute(
                        "UPDATE orchestration_artifacts SET status='verified' WHERE task_id=? AND artifact_id=? AND status='staged'",
                        (state.task_id, artifact_id),
                    )
                conn.execute(
                    "UPDATE orchestration_artifacts SET status='superseded' WHERE task_id=? AND status='staged'",
                    (state.task_id,),
                )
                conn.execute("UPDATE orchestration_runs SET active_task_id=NULL WHERE run_id=?", (state.run_id,))
            self.save_state(conn, state)
            event = "verification_passed" if passed else "verification_failed"
            self.event(conn, state.run_id, event, "独立验收通过。" if passed else "交付件未通过独立验收。", task_id=state.task_id)
            if passed:
                self.event(conn, state.run_id, "task_completed", "任务交付件已发布。", task_id=state.task_id)

    def last_verification(self, task_id: str) -> dict | None:
        return self.db.fetch_one(
            """SELECT v.* FROM orchestration_verifications v JOIN orchestration_actions a ON a.action_id=v.action_id
               WHERE a.task_id=? ORDER BY a.decision_sequence DESC, v.scope LIMIT 1""", (task_id,),
        )

    def verifications(self, task_id: str) -> list[dict]:
        rows = self.db.fetch_all(
            """SELECT v.* FROM orchestration_verifications v JOIN orchestration_actions a ON a.action_id=v.action_id
               WHERE a.task_id=? ORDER BY a.decision_sequence, v.scope""", (task_id,),
        )
        return [
            {key: value for key, value in row.items() if key not in {"result_json", "artifact_ids_json"}}
            | {"result": json.loads(row["result_json"]), "artifact_ids": json.loads(row["artifact_ids_json"])}
            for row in rows
        ]

    def open_wait(
        self, conn: Connection, run_id: str, pending: dict, task_id: str | None = None,
        action_id: str | None = None, status: str = "waiting_for_user",
    ) -> dict:
        wait = {**pending, "wait_id": str(uuid4()), "task_id": task_id}
        conn.execute(
            """INSERT INTO orchestration_waits(wait_id, run_id, task_id, action_id, status, request_json)
               VALUES (?, ?, ?, ?, 'open', ?)""", (wait["wait_id"], run_id, task_id, action_id, self.db.dumps(wait)),
        )
        self.update_run(conn, run_id, status, pending=wait)
        self.event(conn, run_id, "input_required", wait["message"], task_id=task_id)
        return wait

    def resume(
        self, run_id: str, decision: RunResumeRequest, request: ChatRequest, primary: AgentRole, session_id: str,
    ) -> bool:
        with self.db.transaction() as conn:
            if decision.client_request_id:
                previous = conn.execute(
                    "SELECT decision_json FROM orchestration_waits WHERE run_id=? AND client_request_id=?",
                    (run_id, decision.client_request_id),
                ).fetchone()
                if previous is not None:
                    if previous["decision_json"] != decision.model_dump_json():
                        raise ValueError("Resume request ID was already used")
                    return False
            wait = conn.execute(
                "SELECT * FROM orchestration_waits WHERE run_id=? AND wait_id=? AND status='open'",
                (run_id, decision.wait_id),
            ).fetchone()
            if wait is None:
                raise ValueError("Run is not waiting for this decision")
            pending = json.loads(wait["request_json"])
            approval_wait = pending["kind"] == "tool_approval"
            if approval_wait:
                if decision.action not in {"approve", "reject"} or not pending.get("approval"):
                    raise ValueError("This wait requires approval or rejection of the recorded action")
                action = conn.execute(
                    "SELECT status FROM orchestration_actions WHERE action_id=?", (wait["action_id"],),
                ).fetchone()
                if action is None or action["status"] != "waiting_approval":
                    raise ValueError("The action no longer awaits approval")
                if decision.action == "approve":
                    conn.execute("UPDATE orchestration_actions SET status='selected' WHERE action_id=?", (wait["action_id"],))
                else:
                    rejected = TaskActionResult(
                        outcome="failed", summary="用户拒绝了真实 Lint 执行。", error_code="approval_rejected",
                    )
                    conn.execute(
                        "UPDATE orchestration_actions SET status='done', result_json=? WHERE action_id=?",
                        (rejected.model_dump_json(), wait["action_id"]),
                    )
            elif decision.action != "provide_resolution":
                raise ValueError("This wait requires provide_resolution")
            user_message = {
                "approve": "已批准本次真实 Lint。", "reject": "已拒绝本次真实 Lint。",
            }.get(decision.action, "已补充工程输入。")
            message = self.sessions.add_message(
                session_id, "user", decision.content or user_message,
                agent_role=primary.value, message_type="run_decision", run_id=run_id,
                metadata={"wait_id": decision.wait_id, "rtl_path": decision.rtl_path},
                transaction=conn,
            )
            conn.execute(
                """UPDATE orchestration_waits SET status='resolved', decision_json=?, decision_message_id=?,
                   client_request_id=? WHERE wait_id=?""",
                (decision.model_dump_json(), message.message_id, decision.client_request_id, decision.wait_id),
            )
            conn.execute(
                """UPDATE orchestration_runs SET request_json=?, revision=revision+1, turn_id=? WHERE run_id=?""",
                (request.model_dump_json(exclude={"documents", "history", "ds_markdown", "tool_arguments", "user_approved"}),
                 message.turn_id, run_id),
            )
            conn.execute("UPDATE runs SET workspace_id=? WHERE run_id=?", (request.workspace_id, run_id))
            if wait["task_id"]:
                row = conn.execute(
                    "SELECT state_json FROM orchestration_tasks WHERE task_id=?", (wait["task_id"],),
                ).fetchone()
                state = TaskState.model_validate_json(row["state_json"])
                self.save_state(conn, state.model_copy(update={"status": "running", "phase": "selecting", "wait_id": None}))
            self.update_run(conn, run_id, "running")
            self.event(conn, run_id, "run_resumed", user_message, primary.value, wait["task_id"])
        return True

    def decisions(self, run_id: str, task_id: str | None = None) -> list[dict]:
        return [json.loads(row["decision_json"]) for row in self.db.fetch_all(
            """SELECT decision_json FROM orchestration_waits WHERE run_id=? AND status='resolved'
               AND (? IS NULL OR task_id=?) ORDER BY rowid""", (run_id, task_id, task_id),
        )]

    def post_response(self, conn: Connection, run_id: str, message: str) -> None:
        row = conn.execute(
            """SELECT r.session_id, r.primary_agent, h.turn_id FROM runs r
               JOIN orchestration_runs h ON r.run_id=h.run_id WHERE r.run_id=?""", (run_id,),
        ).fetchone()
        exists = conn.execute(
            "SELECT 1 FROM conversation_messages WHERE session_id=? AND turn_id=? AND role='assistant'",
            (row["session_id"], row["turn_id"]),
        ).fetchone()
        if not exists:
            self.sessions.add_message(
                row["session_id"], "assistant", message, agent_role=row["primary_agent"],
                run_id=run_id, turn_id=row["turn_id"], transaction=conn,
            )

    @staticmethod
    def update_run(conn: Connection, run_id: str, status: str, *, pending: dict | None = None, result: dict | None = None) -> None:
        conn.execute(
            """UPDATE runs SET status=?, pending_user_request_json=?, result_json=COALESCE(?, result_json),
               updated_at=? WHERE run_id=?""",
            (status, json.dumps(pending, ensure_ascii=False) if pending else None,
             json.dumps(result, ensure_ascii=False) if result else None, utc_now(), run_id),
        )

    @staticmethod
    def event(
        conn: Connection, run_id: str, kind: str, message: str, agent: str | None = None, task_id: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO run_events(event_id, run_id, sequence, event_type, message, agent_id, created_at, orchestration_task_id)
               VALUES (?, ?, (SELECT COALESCE(MAX(sequence),0)+1 FROM run_events WHERE run_id=?), ?, ?, ?, ?, ?)""",
            (str(uuid4()), run_id, run_id, kind, message, agent, utc_now(), task_id),
        )

    def staged_payload(
        self, state: TaskState, contract: TaskContract, action_id: str, key: str, content: bytes, metadata: dict,
    ) -> ArtifactPayload:
        spec = next(item for item in contract.outputs if item.key == key)
        digest = hashlib.sha256(content).hexdigest()
        artifact = Artifact(
            artifact_id=str(uuid4()), run_id=state.run_id, name=key, artifact_type=spec.artifact_type,
            source_task_id=state.task_id, source_generation=state.generation, source_action_id=action_id,
            output_key=key, version=self.version(state.task_id, key), content_hash=digest,
            content_ref=ContentRef(backend="managed_blob", object_key=digest, media_type=spec.media_type, byte_size=len(content)),
            status="staged", metadata=metadata, created_at=now(),
        )
        return ArtifactPayload(artifact=artifact, content=content)
