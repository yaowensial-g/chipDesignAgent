"""Execution guards shared by artifact publication and the external ToolRun worker."""

import hashlib
import json
from sqlite3 import Connection


def action_is_current(
    conn: Connection, action_id: str, *, task_id: str | None = None, generation: int | None = None,
    run_id: str | None = None, run_status: str = "running",
) -> bool:
    row = conn.execute(
        """SELECT a.task_id, a.generation, t.run_id FROM orchestration_actions a
           JOIN orchestration_tasks t ON t.task_id=a.task_id
           JOIN orchestration_runs h ON h.run_id=t.run_id
           JOIN runs r ON r.run_id=t.run_id
           JOIN orchestration_plans p ON p.plan_id=a.plan_id AND p.run_id=h.run_id AND p.status='active'
           WHERE a.action_id=? AND h.active_task_id=t.task_id AND r.status=?
             AND a.generation=json_extract(t.state_json, '$.generation')
             AND h.replan_trigger_json IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM orchestration_attempt_bindings b JOIN orchestration_artifacts i USING (artifact_id)
                 WHERE b.task_id=a.task_id AND b.generation=a.generation AND i.status <> 'verified'
             )""",
        (action_id, run_status),
    ).fetchone()
    return row is not None and (
        (task_id is None or row["task_id"] == task_id)
        and (generation is None or row["generation"] == generation)
        and (run_id is None or row["run_id"] == run_id)
    )


def frozen_approval_is_current(
    conn: Connection, action_id: str, target: dict, arguments: dict, *, action_status: str = "dispatching",
) -> bool:
    if not action_is_current(conn, action_id):
        return False
    row = conn.execute(
        """SELECT w.request_json, a.generation, a.selection_json, t.run_id, a.task_id, r.workspace_id
           FROM orchestration_waits w JOIN orchestration_actions a USING(action_id)
           JOIN orchestration_tasks t ON t.task_id=a.task_id JOIN runs r ON r.run_id=t.run_id
           WHERE w.action_id=? AND w.status='resolved' AND a.status=?
             AND EXISTS (SELECT 1 FROM orchestration_attempt_bindings b WHERE b.task_id=a.task_id
                         AND b.generation=a.generation AND b.artifact_id=?)
             AND json_extract(w.decision_json,'$.action')='approve' ORDER BY w.rowid DESC LIMIT 1""",
        (action_id, action_status, target.get("snapshot_ref")),
    ).fetchone()
    if row is None:
        return False
    approval = json.loads(row["request_json"])["approval"]
    selection = json.loads(row["selection_json"])
    return arguments == selection["arguments"] and approval == {
        "action_id": action_id, "task_id": row["task_id"], "run_id": row["run_id"],
        "generation": row["generation"], "workspace_id": row["workspace_id"], "tool_name": selection["name"],
        "argument_hash": hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "input_hash": hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest(),
    }
