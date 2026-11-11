"""Additive SQLite schema for the single-process hierarchical executor."""

from sqlite3 import Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    root_message_id TEXT NOT NULL REFERENCES conversation_messages(message_id),
    turn_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    snapshot BLOB,
    context_id TEXT REFERENCES context_manifests(context_id),
    plan_json TEXT,
    active_task_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orchestration_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orchestration_tasks_run ON orchestration_tasks(run_id);
CREATE TABLE IF NOT EXISTS orchestration_actions (
    action_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(task_id),
    decision_sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    selection_json TEXT,
    shortlist_json TEXT,
    reflection_json TEXT,
    result_json TEXT,
    UNIQUE (task_id, decision_sequence)
);
CREATE TABLE IF NOT EXISTS orchestration_observations (
    observation_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES orchestration_actions(action_id),
    observation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_tool_runs (
    action_id TEXT PRIMARY KEY REFERENCES orchestration_actions(action_id),
    tool_run_id TEXT NOT NULL UNIQUE REFERENCES tool_runs(run_id),
    target_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eda_tool_results (
    tool_run_id TEXT PRIMARY KEY REFERENCES tool_runs(run_id),
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eda_run_artifacts (
    tool_run_id TEXT NOT NULL REFERENCES tool_runs(run_id),
    name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY(tool_run_id, name)
);
CREATE TABLE IF NOT EXISTS orchestration_blobs (
    content_hash TEXT PRIMARY KEY,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(task_id),
    action_id TEXT NOT NULL REFERENCES orchestration_actions(action_id),
    output_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL REFERENCES orchestration_blobs(content_hash),
    artifact_json TEXT NOT NULL,
    UNIQUE (task_id, output_key, version)
);
CREATE TABLE IF NOT EXISTS orchestration_bindings (
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(task_id),
    input_key TEXT NOT NULL,
    artifact_id TEXT REFERENCES orchestration_artifacts(artifact_id),
    PRIMARY KEY (task_id, input_key)
);
CREATE TABLE IF NOT EXISTS orchestration_verifications (
    verification_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES orchestration_actions(action_id),
    scope TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (action_id, scope)
);
CREATE TABLE IF NOT EXISTS orchestration_waits (
    wait_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    task_id TEXT REFERENCES orchestration_tasks(task_id),
    action_id TEXT REFERENCES orchestration_actions(action_id),
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    decision_json TEXT,
    decision_message_id TEXT REFERENCES conversation_messages(message_id),
    client_request_id TEXT,
    UNIQUE (run_id, client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_open_wait
    ON orchestration_waits(run_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS orchestration_plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    UNIQUE (run_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_active_plan
    ON orchestration_plans(run_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS orchestration_task_attempts (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(task_id),
    generation INTEGER NOT NULL,
    plan_id TEXT NOT NULL REFERENCES orchestration_plans(plan_id),
    state_json TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, generation)
);
CREATE TABLE IF NOT EXISTS orchestration_attempt_bindings (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    input_key TEXT NOT NULL,
    artifact_id TEXT REFERENCES orchestration_artifacts(artifact_id),
    PRIMARY KEY (run_id, task_id, generation, input_key),
    FOREIGN KEY (run_id, task_id, generation) REFERENCES orchestration_task_attempts(run_id, task_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_artifact_consumers
    ON orchestration_attempt_bindings(artifact_id);
CREATE TABLE IF NOT EXISTS orchestration_replans (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    attempt_no INTEGER NOT NULL,
    old_plan_id TEXT NOT NULL REFERENCES orchestration_plans(plan_id),
    new_plan_id TEXT REFERENCES orchestration_plans(plan_id),
    trigger_json TEXT NOT NULL,
    status TEXT NOT NULL,
    diagnostics TEXT,
    delta_json TEXT,
    affected_tasks_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS orchestration_late_results (
    result_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES orchestration_actions(action_id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def migrate_replanning(conn: Connection) -> None:
    additions = {
        "orchestration_runs": {"planning_policy_json": "TEXT", "replan_trigger_json": "TEXT"},
        "orchestration_actions": {
            "generation": "INTEGER NOT NULL DEFAULT 1", "generation_sequence": "INTEGER NOT NULL DEFAULT 1",
            "plan_id": "TEXT REFERENCES orchestration_plans(plan_id)",
        },
    }
    for table, fields in additions.items():
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in fields.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("""UPDATE orchestration_tasks SET state_json=json_set(state_json,'$.generation',1)
                    WHERE json_extract(state_json,'$.generation') IS NULL""")
    conn.execute("""UPDATE orchestration_artifacts SET artifact_json=json_set(artifact_json,'$.source_generation',1)
                    WHERE json_extract(artifact_json,'$.source_generation') IS NULL""")
    conn.execute("""UPDATE orchestration_observations SET observation_json=json_set(observation_json,'$.generation',1)
                    WHERE json_extract(observation_json,'$.generation') IS NULL""")
    conn.execute("""UPDATE orchestration_actions SET reflection_json=json_set(reflection_json,'$.generation',1)
                    WHERE reflection_json IS NOT NULL AND json_extract(reflection_json,'$.generation') IS NULL""")
    conn.execute("""INSERT OR IGNORE INTO orchestration_plans
        SELECT json_extract(plan_json, '$.plan_id'), run_id, json_extract(plan_json, '$.version'),
               json_extract(plan_json, '$.status'), plan_json
        FROM orchestration_runs WHERE plan_json IS NOT NULL""")
    conn.execute("""INSERT OR IGNORE INTO orchestration_task_attempts
        SELECT t.run_id, t.task_id, COALESCE(json_extract(t.state_json, '$.generation'), 1),
               json_extract(h.plan_json, '$.plan_id'), t.state_json
        FROM orchestration_tasks t JOIN orchestration_runs h USING (run_id) WHERE h.plan_json IS NOT NULL""")
    conn.execute("""INSERT OR IGNORE INTO orchestration_attempt_bindings
        SELECT t.run_id, b.task_id, 1, b.input_key, b.artifact_id
        FROM orchestration_bindings b JOIN orchestration_tasks t USING (task_id)""")
    conn.execute("""UPDATE orchestration_actions AS a SET generation_sequence=decision_sequence,
        plan_id=(SELECT x.plan_id FROM orchestration_task_attempts x WHERE x.task_id=a.task_id AND x.generation=1)
        WHERE plan_id IS NULL""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_generation_sequence
        ON orchestration_actions(task_id, generation, generation_sequence)""")
