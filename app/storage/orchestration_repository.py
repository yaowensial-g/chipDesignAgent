"""Transactional checkpoints for the hierarchical Run engine."""

import hashlib
import json
from datetime import datetime, timezone
from sqlite3 import Connection
from uuid import uuid4

from app.domain.enums import AgentRole
from app.domain.models import ChatRequest, RunResumeRequest, ToolSelection
from app.domain.orchestration import (
    ActionApproval, Artifact, ArtifactPayload, ContentRef, Observation, PlanDelta, ReflectionRecord, ReplanTrigger, TaskActionResult,
    TaskContract, TaskOutputRef, TaskPlan, TaskState, VerificationResult,
)
from app.services.dependency_tracker_service import DependencyTrackerService
from app.services.session_service import SessionService
from app.services.workspace_service import utc_now
from app.storage.database import Database
from app.storage.orchestration_fence import action_is_current, frozen_approval_is_current


def now() -> datetime:
    return datetime.now(timezone.utc)


class StaleExecution(ValueError):
    """A result no longer belongs to the active plan and attempt."""


class OrchestrationRepository:
    def __init__(self, db: Database, sessions: SessionService) -> None:
        self.db = db
        self.sessions = sessions
        self.dependencies = DependencyTrackerService()

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
               JOIN orchestration_runs h ON h.run_id=t.run_id AND h.active_task_id=t.task_id
               JOIN orchestration_plans p ON p.plan_id=a.plan_id AND p.status='active'
               JOIN runs r ON r.run_id=t.run_id WHERE a.status='dispatching' AND r.status='running'
                 AND a.generation=json_extract(t.state_json,'$.generation')""",
        )]

    def mark_external(self, state: TaskState, tool_run_id: str, tool_name: str = "verilator_lint") -> None:
        if state.phase == "awaiting_external":
            return
        with self.db.transaction() as conn:
            link = conn.execute("SELECT action_id FROM orchestration_tool_runs WHERE tool_run_id=?", (tool_run_id,)).fetchone()
            if link is None:
                raise StaleExecution(tool_run_id)
            self._require_current(conn, state, link["action_id"])
            self.save_state(conn, state.model_copy(update={"phase": "awaiting_external"}))
            key = "lint_tool_run_id" if tool_name == "verilator_lint" else "eda_tool_run_id"
            self.update_run(conn, state.run_id, "running", result={key: tool_run_id})
            self.event(conn, state.run_id, "tool_started", f"{tool_name} ToolRun {tool_run_id}", task_id=state.task_id)

    def latest_tool_run_id(self, run_id: str) -> str | None:
        row = self.db.fetch_one(
            """SELECT link.tool_run_id FROM orchestration_tool_runs link
               JOIN orchestration_actions a ON a.action_id=link.action_id
               JOIN orchestration_tasks t ON t.task_id=a.task_id WHERE t.run_id=?
               ORDER BY a.decision_sequence DESC LIMIT 1""", (run_id,),
        )
        return row["tool_run_id"] if row else None

    def tool_run_ids(self, run_id: str) -> list[str]:
        return [row["tool_run_id"] for row in self.db.fetch_all(
            """SELECT link.tool_run_id FROM orchestration_tool_runs link
               JOIN orchestration_actions a ON a.action_id=link.action_id
               JOIN orchestration_tasks t ON t.task_id=a.task_id WHERE t.run_id=? ORDER BY link.rowid""", (run_id,),
        )]

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

    def save_plan(self, plan: TaskPlan, planning_policy_json: str | None = None) -> None:
        with self.db.transaction() as conn:
            self._insert_plan(conn, plan)
            conn.execute("UPDATE orchestration_runs SET planning_policy_json=? WHERE run_id=?", (planning_policy_json, plan.run_id))
            for task in plan.tasks:
                state = TaskState(run_id=plan.run_id, task_id=task.task_id, generation=1, status="pending")
                self._new_attempt(conn, state, plan.plan_id, new_task=True)
            self.update_run(conn, plan.run_id, "running")
            self.event(conn, plan.run_id, "plan_created", f"已建立 {len(plan.tasks)} 个任务。")
            self.event(conn, plan.run_id, "plan_validated", "任务依赖与能力范围已通过校验。")

    @staticmethod
    def _insert_plan(conn: Connection, plan: TaskPlan) -> None:
        payload = plan.model_dump_json()
        conn.execute("INSERT INTO orchestration_plans VALUES (?, ?, ?, ?, ?)",
                     (plan.plan_id, plan.run_id, plan.version, plan.status, payload))
        conn.execute("UPDATE orchestration_runs SET plan_json=? WHERE run_id=?", (payload, plan.run_id))

    @staticmethod
    def close_plan(conn: Connection, run_id: str) -> None:
        conn.execute("""UPDATE orchestration_plans SET status='closed', plan_json=json_set(plan_json,'$.status','closed')
                        WHERE run_id=? AND status='active'""", (run_id,))
        conn.execute("""UPDATE orchestration_runs SET active_task_id=NULL, replan_trigger_json=NULL,
                        plan_json=json_set(plan_json,'$.status','closed') WHERE run_id=?""", (run_id,))
        conn.execute("""UPDATE orchestration_replans SET status='interrupted',diagnostics='run_closed_during_proposal'
                        WHERE run_id=? AND status='proposing'""", (run_id,))

    def plans(self, run_id: str) -> list[TaskPlan]:
        return [TaskPlan.model_validate_json(row["plan_json"]) for row in self.db.fetch_all(
            "SELECT plan_json FROM orchestration_plans WHERE run_id=? ORDER BY version", (run_id,),
        )]

    def attempts(self, run_id: str, task_id: str) -> list[dict]:
        return [TaskState.model_validate_json(row["state_json"]).model_dump(mode="json") | {"plan_id": row["plan_id"]}
                for row in self.db.fetch_all(
                    """SELECT plan_id,state_json FROM orchestration_task_attempts
                       WHERE run_id=? AND task_id=? ORDER BY generation""", (run_id, task_id),
                )]

    def usage(self, run_id: str) -> tuple[int, int]:
        row = self.db.fetch_one(
            """SELECT COALESCE(SUM(json_extract(state_json,'$.action_count')),0) AS actions,
                      COALESCE(SUM(json_extract(state_json,'$.current_iteration')),0) AS decisions
               FROM orchestration_task_attempts WHERE run_id=?""", (run_id,),
        )
        return row["actions"], row["decisions"]

    def replan_history(self, run_id: str) -> list[dict]:
        return [
            {key: value for key, value in row.items() if not key.endswith("_json")}
            | {key.removesuffix("_json"): json.loads(value) if value else None
               for key, value in row.items() if key.endswith("_json")}
            for row in self.db.fetch_all("SELECT * FROM orchestration_replans WHERE run_id=? ORDER BY attempt_no", (run_id,))
        ]

    def replan_count(self, run_id: str) -> int:
        return self.db.fetch_one("SELECT COUNT(*) AS count FROM orchestration_replans WHERE run_id=?", (run_id,))["count"]

    def _set_trigger(self, conn: Connection, run_id: str, trigger: ReplanTrigger) -> None:
        if conn.execute(
            "UPDATE orchestration_runs SET replan_trigger_json=? WHERE run_id=? AND replan_trigger_json IS NULL",
            (trigger.model_dump_json(), run_id),
        ).rowcount != 1:
            raise StaleExecution(trigger.task_id)
        self.event(conn, run_id, "replan_requested", trigger.reason, task_id=trigger.task_id)

    def request_replan(self, plan: TaskPlan, state: TaskState, trigger: ReplanTrigger) -> None:
        with self.db.transaction() as conn:
            valid = conn.execute(
                """SELECT 1 FROM orchestration_plans p JOIN orchestration_runs h USING(run_id)
                   JOIN runs r USING(run_id) JOIN orchestration_tasks t ON t.task_id=h.active_task_id
                   WHERE p.plan_id=? AND p.status='active' AND r.status='running'
                     AND t.task_id=? AND json_extract(t.state_json,'$.generation')=?""",
                (plan.plan_id, state.task_id, state.generation),
            ).fetchone()
            if valid is None:
                raise StaleExecution(state.task_id)
            self._set_trigger(conn, plan.run_id, self._apply_invalidation(conn, plan.run_id, trigger))

    def request_invalidation_confirmation(self, state: TaskState, action_id: str, trigger: ReplanTrigger) -> None:
        with self.db.transaction() as conn:
            self._require_current(conn, state, action_id)
            pending = self.open_wait(conn, state.run_id, {
                "kind": "artifact_invalidation", "trigger": trigger.model_dump(mode="json"),
                "message": f"请确认是否将 Artifact {', '.join(trigger.invalidated_artifact_ids)} 标记为失效。{trigger.reason}",
            }, state.task_id, action_id)
            self.post_response(conn, state.run_id, pending["message"])
            self.save_state(conn, state.model_copy(update={"status": "waiting_for_user", "phase": None,
                                                          "wait_id": pending["wait_id"]}))

    def _apply_invalidation(self, conn: Connection, run_id: str, trigger: ReplanTrigger) -> ReplanTrigger:
        if not trigger.invalidated_artifact_ids:
            return trigger
        ids = set(trigger.invalidated_artifact_ids)
        for artifact_id in ids:
            if conn.execute(
                """UPDATE orchestration_artifacts SET status='invalidated' WHERE artifact_id=? AND status='verified'
                   AND task_id IN (SELECT task_id FROM orchestration_tasks WHERE run_id=?)""", (artifact_id, run_id),
            ).rowcount != 1:
                raise StaleExecution(artifact_id)
        impact = self.dependencies.impact(conn, run_id, ids)
        conn.executemany("UPDATE orchestration_artifacts SET status='invalidated' WHERE artifact_id=? AND status='verified'",
                         ((key,) for key in sorted(impact.artifacts - ids)))
        self.event(conn, run_id, "artifacts_invalidated", ", ".join(sorted(impact.artifacts)), task_id=trigger.task_id)
        return trigger.model_copy(update={"required_reruns": tuple(sorted(impact.reruns))})

    def reserve_replan(self, plan: TaskPlan, trigger: ReplanTrigger, limit: int) -> tuple[int | None, str | None]:
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT h.replan_trigger_json FROM orchestration_runs h JOIN orchestration_plans p USING(run_id)
                   JOIN runs r USING(run_id) WHERE p.plan_id=? AND p.status='active' AND r.status='running'""",
                (plan.plan_id,),
            ).fetchone()
            if row is None or row["replan_trigger_json"] != trigger.model_dump_json():
                raise StaleExecution(plan.plan_id)
            conn.execute(
                """UPDATE orchestration_replans SET status='interrupted', diagnostics='proposal_interrupted'
                   WHERE run_id=? AND status='proposing'""", (plan.run_id,),
            )
            last = conn.execute("SELECT attempt_no,diagnostics FROM orchestration_replans WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
                                (plan.run_id,)).fetchone()
            number = (last["attempt_no"] if last else 0) + 1
            feedback = last["diagnostics"] if last else None
            if number > limit:
                return None, feedback
            conn.execute(
                """INSERT INTO orchestration_replans(run_id,attempt_no,old_plan_id,trigger_json,status,created_at,affected_tasks_json)
                   VALUES (?, ?, ?, ?, 'proposing', ?, ?)""",
                (plan.run_id, number, plan.plan_id, row["replan_trigger_json"], utc_now(), self.db.dumps(sorted({
                    trigger.task_id, *trigger.required_reruns, *(trigger.issue.affected_task_ids if trigger.issue else ()),
                }))),
            )
            self.event(conn, plan.run_id, "replanning_started", f"提案 {number}。", task_id=trigger.task_id)
            return number, feedback

    def reject_replan(self, plan: TaskPlan, number: int, diagnostic: str, delta: PlanDelta | None) -> None:
        with self.db.transaction() as conn:
            changed = conn.execute(
                """UPDATE orchestration_replans SET status='rejected', diagnostics=?, delta_json=?
                   WHERE run_id=? AND attempt_no=? AND old_plan_id=? AND status='proposing'""",
                (diagnostic, delta.model_dump_json() if delta else None, plan.run_id, number, plan.plan_id),
            ).rowcount
            if changed:
                self.event(conn, plan.run_id, "replan_rejected", diagnostic)

    def publish_replan(
        self, previous: TaskPlan, plan: TaskPlan, number: int, delta: PlanDelta,
        trigger: ReplanTrigger, reruns: set[str], removed: set[str],
    ) -> None:
        with self.db.transaction() as conn:
            ticket = conn.execute(
                """SELECT 1 FROM orchestration_replans a JOIN orchestration_plans p ON p.plan_id=a.old_plan_id
                   JOIN orchestration_runs h ON h.run_id=a.run_id JOIN runs r ON r.run_id=a.run_id
                   WHERE a.run_id=? AND a.attempt_no=? AND p.plan_id=? AND p.status='active'
                     AND a.status='proposing' AND r.status='running' AND h.replan_trigger_json=a.trigger_json
                     AND a.trigger_json=?""", (previous.run_id, number, previous.plan_id, trigger.model_dump_json()),
            ).fetchone()
            if not ticket:
                raise StaleExecution(previous.plan_id)
            conn.execute("""UPDATE orchestration_plans SET status='superseded', plan_json=json_set(plan_json,'$.status','superseded')
                            WHERE plan_id=?""", (previous.plan_id,))
            self._insert_plan(conn, plan)
            states = {state.task_id: state for state in (
                TaskState.model_validate_json(row["state_json"]) for row in conn.execute(
                    "SELECT state_json FROM orchestration_tasks WHERE run_id=?", (plan.run_id,),
                )
            )}
            for key in reruns | removed:
                old = states[key]
                if old.status not in {"completed", "failed"}:
                    self.save_state(conn, old.model_copy(update={"status": "cancelled", "phase": None, "wait_id": None,
                                                                  "completed_at": now(), "failure_reason": trigger.reason}))
                conn.execute("""UPDATE orchestration_artifacts SET status='rejected' WHERE task_id=? AND status='staged'
                                AND json_extract(artifact_json,'$.source_generation')=?""", (key, old.generation))
            for task in plan.tasks:
                old = states.get(task.task_id)
                if old is None or task.task_id in reruns:
                    self._new_attempt(conn, TaskState(
                        run_id=plan.run_id, task_id=task.task_id, generation=old.generation + 1 if old else 1,
                        status="pending", repair_attempts=old.repair_attempts if old else 0,
                    ), plan.plan_id, new_task=old is None)
            conn.execute("UPDATE orchestration_waits SET status='cancelled' WHERE run_id=? AND status='open'", (plan.run_id,))
            conn.execute("UPDATE orchestration_runs SET active_task_id=NULL,replan_trigger_json=NULL WHERE run_id=?", (plan.run_id,))
            self.update_run(conn, plan.run_id, "running")
            conn.execute(
                """UPDATE orchestration_replans SET status='applied',new_plan_id=?,delta_json=?,affected_tasks_json=?
                   WHERE run_id=? AND attempt_no=?""", (plan.plan_id, delta.model_dump_json(), self.db.dumps(plan.affected_tasks),
                                                        plan.run_id, number),
            )
            self.event(conn, plan.run_id, "plan_updated", f"计划 v{plan.version} 已生效。")

    def _new_attempt(self, conn: Connection, state: TaskState, plan_id: str, *, new_task: bool = False) -> None:
        if new_task:
            conn.execute("INSERT INTO orchestration_tasks VALUES (?, ?, ?)", (state.task_id, state.run_id, state.model_dump_json()))
        conn.execute("INSERT INTO orchestration_task_attempts VALUES (?, ?, ?, ?, ?)",
                     (state.run_id, state.task_id, state.generation, plan_id, state.model_dump_json()))
        self.save_state(conn, state)

    def states(self, run_id: str) -> dict[str, TaskState]:
        rows = self.db.fetch_all("SELECT state_json FROM orchestration_tasks WHERE run_id = ?", (run_id,))
        states = [TaskState.model_validate_json(row["state_json"]) for row in rows]
        return {state.task_id: state for state in states}

    @staticmethod
    def save_state(conn: Connection, state: TaskState) -> None:
        payload = state.model_dump_json()
        conn.execute("UPDATE orchestration_tasks SET state_json = ? WHERE task_id = ?", (payload, state.task_id))
        conn.execute("""UPDATE orchestration_task_attempts SET state_json=? WHERE run_id=? AND task_id=? AND generation=?""",
                     (payload, state.run_id, state.task_id, state.generation))

    def claim(self, state: TaskState, contract: TaskContract, plan_id: str | None = None) -> TaskState:
        with self.db.transaction() as conn:
            plan = conn.execute("SELECT plan_id,plan_json FROM orchestration_plans WHERE run_id=? AND status='active'",
                                (state.run_id,)).fetchone()
            task = conn.execute("SELECT state_json FROM orchestration_tasks WHERE task_id=? AND run_id=?",
                                (state.task_id, state.run_id)).fetchone()
            if (plan is None or (plan_id and plan["plan_id"] != plan_id) or task is None
                or TaskState.model_validate_json(task["state_json"]) != state or state.status != "pending"
                or contract not in TaskPlan.model_validate_json(plan["plan_json"]).tasks):
                raise StaleExecution(state.task_id)
            for spec in contract.inputs:
                row = None
                if isinstance(spec.source, TaskOutputRef):
                    row = conn.execute(
                        """SELECT artifact_id FROM orchestration_artifacts a JOIN orchestration_tasks t USING(task_id)
                           WHERE a.task_id = ? AND output_key = ? AND a.status = 'verified'
                             AND json_extract(t.state_json,'$.status')='completed'
                             AND json_extract(t.state_json,'$.generation')=json_extract(a.artifact_json,'$.source_generation')
                           ORDER BY version DESC LIMIT 1""",
                        (spec.source.task_id, spec.source.output_key),
                    ).fetchone()
                if row is None and spec.required:
                    raise ValueError(f"Required verified input is unavailable: {spec.key}")
                conn.execute(
                    "INSERT INTO orchestration_attempt_bindings VALUES (?, ?, ?, ?, ?)",
                    (state.run_id, state.task_id, state.generation, spec.key, row["artifact_id"] if row else None),
                )
            claimed = conn.execute(
                "UPDATE orchestration_runs SET active_task_id = ? WHERE run_id = ? AND active_task_id IS NULL",
                (state.task_id, state.run_id),
            ).rowcount
            if claimed != 1:
                raise ValueError("Run already has an active task")
            conn.execute("UPDATE orchestration_task_attempts SET plan_id=? WHERE run_id=? AND task_id=? AND generation=?",
                         (plan["plan_id"], state.run_id, state.task_id, state.generation))
            state = state.model_copy(update={"status": "running", "phase": "selecting", "started_at": now()})
            self.save_state(conn, state)
            self.event(conn, state.run_id, "task_started", contract.title, contract.assigned_agent.value, state.task_id)
        return state

    def reserve(self, state: TaskState) -> tuple[TaskState, str]:
        action_id = str(uuid4())
        state = state.model_copy(update={"current_iteration": state.current_iteration + 1})
        with self.db.transaction() as conn:
            plan = conn.execute(
                """SELECT p.plan_id FROM orchestration_plans p JOIN orchestration_runs h USING(run_id)
                   JOIN orchestration_tasks t ON t.task_id=h.active_task_id JOIN runs r USING(run_id)
                   WHERE p.run_id=? AND p.status='active' AND t.task_id=? AND r.status='running'
                     AND h.replan_trigger_json IS NULL AND json_extract(t.state_json,'$.generation')=?
                     AND json_extract(t.state_json,'$.current_iteration')=?""",
                (state.run_id, state.task_id, state.generation, state.current_iteration - 1),
            ).fetchone()
            if plan is None:
                raise StaleExecution(state.task_id)
            conn.execute(
                """INSERT INTO orchestration_actions(action_id, task_id, decision_sequence, generation,
                       generation_sequence, plan_id, status)
                   VALUES (?, ?, (SELECT COALESCE(MAX(decision_sequence),0)+1 FROM orchestration_actions WHERE task_id=?),
                           ?, ?, ?, 'selecting')""",
                (action_id, state.task_id, state.task_id, state.generation, state.current_iteration, plan["plan_id"]),
            )
            self.save_state(conn, state)
        return state, action_id

    def last_action(self, task_id: str, generation: int = 1) -> dict | None:
        return self.db.fetch_one(
            """SELECT * FROM orchestration_actions WHERE task_id=? AND generation=?
               ORDER BY generation_sequence DESC LIMIT 1""", (task_id, generation),
        )

    def select(
        self, state: TaskState, selection: ToolSelection, shortlist: list[str], reflection: ReflectionRecord,
    ) -> None:
        with self.db.transaction() as conn:
            self._require_current(conn, state, reflection.action_id)
            updated = conn.execute(
                """UPDATE orchestration_actions SET status='selected', selection_json=?, shortlist_json=?, reflection_json=?
                   WHERE action_id=? AND status='selecting'""",
                (selection.model_dump_json(), self.db.dumps(shortlist), reflection.model_dump_json(), reflection.action_id),
            ).rowcount
            if not updated:
                raise StaleExecution(reflection.action_id)
            self.event(conn, state.run_id, "action_selected", selection.name, task_id=state.task_id)
            self.event(conn, state.run_id, "reflection_recorded", reflection.result.progress_summary, task_id=state.task_id)

    def dispatch(self, state: TaskState, action_id: str, count: bool) -> TaskState:
        state = state.model_copy(update={"action_count": state.action_count + int(count), "phase": "dispatching"})
        with self.db.transaction() as conn:
            self._require_current(conn, state, action_id)
            changed = conn.execute(
                "UPDATE orchestration_actions SET status='dispatching' WHERE action_id=? AND status='selected'",
                (action_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("Action has already been dispatched")
            self.save_state(conn, state)
        return state

    def observations(self, task_id: str, generation: int = 1) -> tuple[Observation, ...]:
        return tuple(Observation.model_validate_json(row["observation_json"]) for row in self.db.fetch_all(
            """SELECT o.observation_json FROM orchestration_observations o JOIN orchestration_actions a
               ON a.action_id=o.action_id WHERE a.task_id=? AND a.generation=? ORDER BY a.generation_sequence""",
            (task_id, generation),
        ))

    def reflections(self, task_id: str, generation: int = 1) -> list[ReflectionRecord]:
        return [ReflectionRecord.model_validate_json(row["reflection_json"]) for row in self.db.fetch_all(
            """SELECT reflection_json FROM orchestration_actions WHERE task_id=? AND generation=?
               AND reflection_json IS NOT NULL ORDER BY generation_sequence""", (task_id, generation),
        )]

    def bound_inputs(self, task_id: str, generation: int = 1, *, include_superseded: bool = False) -> tuple[ArtifactPayload, ...]:
        return self._artifacts(
            """JOIN orchestration_attempt_bindings i ON i.artifact_id=a.artifact_id
               WHERE i.task_id=? AND i.generation=? AND (a.status='verified' OR (? AND a.status='superseded'))
               ORDER BY i.input_key""", (task_id, generation, int(include_superseded)),
        )

    def input_bindings(self, task_id: str, generation: int) -> list[dict]:
        return self.db.fetch_all(
            """SELECT b.input_key, b.artifact_id, a.status FROM orchestration_attempt_bindings b
               LEFT JOIN orchestration_artifacts a USING(artifact_id) WHERE b.task_id=? AND b.generation=?
               ORDER BY b.input_key""", (task_id, generation),
        )

    def artifact_history(self, task_id: str, generation: int) -> list[Artifact]:
        return [Artifact.model_validate_json(row["artifact_json"]).model_copy(update={"status": row["status"]})
                for row in self.db.fetch_all(
                    """SELECT artifact_json,status FROM orchestration_artifacts WHERE task_id=?
                       AND json_extract(artifact_json,'$.source_generation')=? ORDER BY output_key,version""",
                    (task_id, generation),
                )]

    def action_history(self, task_id: str, generation: int) -> list[dict]:
        return self.db.fetch_all(
            """SELECT action_id, plan_id, generation_sequence AS decision_sequence, status,
                      json_extract(selection_json,'$.name') AS tool_name
               FROM orchestration_actions WHERE task_id=? AND generation=? ORDER BY generation_sequence""",
            (task_id, generation),
        )

    def late_results(self, task_id: str, generation: int) -> list[dict]:
        return [
            {"action_id": row["action_id"], "created_at": row["created_at"], "result": json.loads(row["result_json"])}
            for row in self.db.fetch_all(
                """SELECT l.action_id,l.created_at,l.result_json FROM orchestration_late_results l
                   JOIN orchestration_actions a USING(action_id) WHERE a.task_id=? AND a.generation=?
                   ORDER BY l.rowid""", (task_id, generation),
            )
        ]

    def local_outputs(self, task_id: str, status: str, generation: int = 1) -> tuple[ArtifactPayload, ...]:
        return self._artifacts(
            """WHERE a.task_id=? AND a.status=? AND json_extract(a.artifact_json,'$.source_generation')=?
               ORDER BY a.output_key, a.version""", (task_id, status, generation),
        )

    @staticmethod
    def _require_current(conn: Connection, state: TaskState, action_id: str) -> None:
        if not action_is_current(conn, action_id, run_id=state.run_id, task_id=state.task_id, generation=state.generation):
            raise StaleExecution(action_id)

    def _audit_late(self, conn: Connection, state: TaskState, action_id: str, result: dict) -> None:
        conn.execute("INSERT INTO orchestration_late_results VALUES (?, ?, ?, ?)",
                     (str(uuid4()), action_id, self.db.dumps(result), utc_now()))
        self.event(conn, state.run_id, "stale_result_ignored", action_id, task_id=state.task_id)

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
    ) -> bool:
        if (observation.run_id, observation.task_id, observation.generation) != (state.run_id, state.task_id, state.generation):
            raise ValueError("Observation does not belong to this attempt")
        with self.db.transaction() as conn:
            action = conn.execute(
                """SELECT a.status FROM orchestration_actions a JOIN orchestration_tasks t USING(task_id)
                   WHERE a.action_id=? AND a.task_id=? AND t.run_id=?""", (observation.action_id, state.task_id, state.run_id),
            ).fetchone()
            if action is None:
                return False
            external = conn.execute("""SELECT link.target_json, t.arguments_json FROM orchestration_tool_runs link
                                       JOIN tool_runs t ON t.run_id=link.tool_run_id WHERE link.action_id=?""",
                                    (observation.action_id,)).fetchone()
            current = action_is_current(conn, observation.action_id, task_id=state.task_id, generation=state.generation)
            if not current:
                self._audit_late(conn, state, observation.action_id, {"observation": observation.model_dump(mode="json"),
                                                                   "result": result,
                                                                   "artifacts": [item.artifact.model_dump(mode="json") for item in staged]})
                return False
            action_status = action["status"]
            if action_status not in {"selecting", "selected", "dispatching"}:
                return False
            if external and action_status == "dispatching" and not frozen_approval_is_current(
                conn, observation.action_id, json.loads(external["target_json"]), json.loads(external["arguments_json"]),
            ):
                self._audit_late(conn, state, observation.action_id, {"observation": observation.model_dump(mode="json"),
                                                                   "result": result})
                denied = TaskActionResult(outcome="failed", error_code="approval_target_changed",
                                          summary="外部结果的审批已失效，未发布交付件。")
                conn.execute("UPDATE orchestration_actions SET status='done',result_json=? WHERE action_id=?",
                             (denied.model_dump_json(), observation.action_id))
                self.save_state(conn, state.model_copy(update={"phase": "selecting"}))
                return True
            for item in staged:
                artifact = item.artifact
                if (artifact.source_generation, artifact.source_task_id, artifact.source_action_id, artifact.run_id, artifact.status) != (
                    state.generation, state.task_id, observation.action_id, state.run_id, "staged"
                ):
                    raise ValueError("Artifact provenance does not match the dispatch")
                self._store_blob(conn, item)
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
            return True

    @staticmethod
    def _store_blob(conn: Connection, item: ArtifactPayload) -> None:
        digest = hashlib.sha256(item.content).hexdigest()
        if digest != item.artifact.content_hash or digest != item.artifact.content_ref.object_key:
            raise ValueError("Staged blob does not match its digest")
        stored = conn.execute("SELECT content FROM orchestration_blobs WHERE content_hash=?", (digest,)).fetchone()
        if stored is None:
            conn.execute("INSERT INTO orchestration_blobs VALUES (?, ?)", (digest, item.content))
        elif stored["content"] != item.content:
            if hashlib.sha256(stored["content"]).hexdigest() == digest:
                raise ValueError("Distinct blobs share a digest")
            # Restore corrupted storage using the original digest. Existing artifact statuses remain unchanged.
            conn.execute("UPDATE orchestration_blobs SET content=? WHERE content_hash=?", (item.content, digest))

    def record_verification(
        self, state: TaskState, action_id: str, result: VerificationResult, artifact_ids: tuple[str, ...],
        *, global_result: VerificationResult | None = None, trigger: ReplanTrigger | None = None,
    ) -> bool:
        with self.db.transaction() as conn:
            stored = conn.execute(
                """SELECT a.status,a.result_json FROM orchestration_actions a JOIN orchestration_tasks t USING(task_id)
                   WHERE a.action_id=? AND a.task_id=? AND t.run_id=?""", (action_id, state.task_id, state.run_id),
            ).fetchone()
            if stored is None:
                return False
            if not action_is_current(conn, action_id, task_id=state.task_id, generation=state.generation):
                self._audit_late(conn, state, action_id, {"verification": result.model_dump(mode="json")})
                return False
            if stored["status"] != "done" or set(json.loads(stored["result_json"]).get("candidate_artifact_ids", [])) != set(artifact_ids):
                return False
            if conn.execute("SELECT 1 FROM orchestration_verifications WHERE action_id=?", (action_id,)).fetchone():
                return False
            for artifact_id in artifact_ids:
                proof = conn.execute(
                    """SELECT link.action_id,link.target_json,t.arguments_json FROM orchestration_artifacts a
                       JOIN orchestration_tool_runs link USING(action_id) JOIN tool_runs t ON t.run_id=link.tool_run_id
                       WHERE a.artifact_id=?""", (artifact_id,),
                ).fetchone()
                if proof and not frozen_approval_is_current(
                    conn, proof["action_id"], json.loads(proof["target_json"]), json.loads(proof["arguments_json"]), action_status="done",
                ):
                    self._audit_late(conn, state, action_id, {"verification": result.model_dump(mode="json"), "artifact_id": artifact_id})
                    result = VerificationResult(passed=False, failed_criteria=("approval_target_changed",))
                    global_result = None
                    trigger = None
                    state = state.model_copy(update={"status": "failed", "phase": None, "artifact_ids": (),
                                                     "failure_reason": "外部结果的审批已失效，未发布交付件。", "completed_at": now()})
                    break
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
                    if not conn.execute(
                        """SELECT 1 FROM orchestration_artifacts WHERE task_id=? AND artifact_id=? AND status='staged'
                           AND json_extract(artifact_json,'$.source_generation')=?""", (state.task_id, artifact_id, state.generation),
                    ).fetchone():
                        raise ValueError("Verifier output does not belong to the active generation")
                    conn.execute(
                        """UPDATE orchestration_artifacts SET status='superseded' WHERE task_id=? AND status='verified'
                           AND output_key=(SELECT output_key FROM orchestration_artifacts WHERE artifact_id=?)""",
                        (state.task_id, artifact_id),
                    )
                    conn.execute(
                        "UPDATE orchestration_artifacts SET status='verified' WHERE task_id=? AND artifact_id=? AND status='staged'",
                        (state.task_id, artifact_id),
                    )
                conn.execute(
                    """UPDATE orchestration_artifacts SET status='superseded' WHERE task_id=? AND status='staged'
                       AND json_extract(artifact_json,'$.source_generation')=?""", (state.task_id, state.generation),
                )
                conn.execute("UPDATE orchestration_runs SET active_task_id=NULL WHERE run_id=?", (state.run_id,))
            self.save_state(conn, state)
            event = "verification_passed" if passed else "verification_failed"
            self.event(conn, state.run_id, event, "独立验收通过。" if passed else "交付件未通过独立验收。", task_id=state.task_id)
            if passed:
                self.event(conn, state.run_id, "task_completed", "任务交付件已发布。", task_id=state.task_id)
            if trigger is not None:
                self._set_trigger(conn, state.run_id, trigger)
            return True

    def last_verification(self, task_id: str, generation: int = 1) -> dict | None:
        return self.db.fetch_one(
            """SELECT v.* FROM orchestration_verifications v JOIN orchestration_actions a ON a.action_id=v.action_id
               WHERE a.task_id=? AND a.generation=? ORDER BY a.generation_sequence DESC, v.scope LIMIT 1""", (task_id, generation),
        )

    def verifications(self, task_id: str, generation: int = 1) -> list[dict]:
        rows = self.db.fetch_all(
            """SELECT v.* FROM orchestration_verifications v JOIN orchestration_actions a ON a.action_id=v.action_id
               WHERE a.task_id=? AND a.generation=? ORDER BY a.generation_sequence, v.scope""", (task_id, generation),
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
            if pending["kind"] == "tool_approval":
                if decision.action not in {"approve", "reject"} or not pending.get("approval"):
                    raise ValueError("This wait requires approval or rejection of the recorded action")
                action = conn.execute(
                    "SELECT status FROM orchestration_actions WHERE action_id=?", (wait["action_id"],),
                ).fetchone()
                if action is None or action["status"] != "waiting_approval" or not action_is_current(
                    conn, wait["action_id"], run_status="waiting_for_approval",
                ):
                    raise ValueError("The action no longer awaits approval")
                if decision.action == "approve":
                    conn.execute("UPDATE orchestration_actions SET status='selected' WHERE action_id=?", (wait["action_id"],))
                else:
                    rejected = TaskActionResult(
                        outcome="failed", summary="用户拒绝了本次工程工具执行。", error_code="approval_rejected",
                    )
                    conn.execute(
                        "UPDATE orchestration_actions SET status='done', result_json=? WHERE action_id=?",
                        (rejected.model_dump_json(), wait["action_id"]),
                    )
            elif pending["kind"] == "artifact_invalidation":
                if decision.action not in {"approve", "reject"}:
                    raise ValueError("This wait requires confirmation or rejection of artifact invalidation")
                if not action_is_current(conn, wait["action_id"], run_status="waiting_for_user"):
                    raise StaleExecution(wait["action_id"])
                if decision.action == "approve":
                    trigger = ReplanTrigger.model_validate(pending["trigger"])
                    self._set_trigger(conn, run_id, self._apply_invalidation(conn, run_id, trigger))
                else:
                    rejected = TaskActionResult(outcome="failed", summary="用户拒绝将已验收交付件标记为失效。",
                                                error_code="invalidation_rejected")
                    conn.execute("UPDATE orchestration_actions SET result_json=? WHERE action_id=?",
                                 (rejected.model_dump_json(), wait["action_id"]))
            elif decision.action != "provide_resolution":
                raise ValueError("This wait requires provide_resolution")
            user_message = {
                "approve": "已批准本次工程工具执行。", "reject": "已拒绝本次工程工具执行。",
            }.get(decision.action, "已补充工程输入。")
            if pending["kind"] == "artifact_invalidation":
                user_message = "已确认 Artifact 失效。" if decision.action == "approve" else "已拒绝 Artifact 失效请求。"
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
