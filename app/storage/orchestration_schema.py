"""Additive SQLite schema for the single-process hierarchical executor."""

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
"""
