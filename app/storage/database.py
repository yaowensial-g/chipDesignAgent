import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "chipflow.db"


class Database:
    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            derived_from_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE TABLE IF NOT EXISTS patches (
            patch_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            target_path TEXT NOT NULL,
            before_content TEXT NOT NULL,
            after_content TEXT NOT NULL,
            reason TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            diff TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE TABLE IF NOT EXISTS tool_runs (
            run_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            command_json TEXT NOT NULL,
            exit_code INTEGER,
            stdout TEXT NOT NULL,
            stderr TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE TABLE IF NOT EXISTS engineering_issues (
            issue_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            queue_index INTEGER NOT NULL,
            stage TEXT NOT NULL,
            tool TEXT NOT NULL,
            severity TEXT NOT NULL,
            code TEXT,
            message TEXT NOT NULL,
            file TEXT,
            line INTEGER,
            raw_log_ref TEXT,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            user_decision_json TEXT,
            FOREIGN KEY(run_id) REFERENCES tool_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS task_snapshots (
            task_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            module_name TEXT NOT NULL,
            workspace_id TEXT,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL,
            user_instruction TEXT NOT NULL,
            route_reason TEXT NOT NULL,
            legacy_task_id TEXT,
            tool_run_id TEXT,
            patch_id TEXT,
            pending_user_request_json TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            agent_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_issues_run_order ON engineering_issues(run_id, queue_index);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_issue_fingerprint ON engineering_issues(run_id, fingerprint);
        CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_order ON run_events(run_id, sequence);
        """
        with self._lock, self.connect() as connection:
            connection.executescript(schema)
            issue_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(engineering_issues)")
            }
            if "analysis_json" not in issue_columns:
                connection.execute(
                    "ALTER TABLE engineering_issues ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.commit()

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        return json.loads(value)


database = Database()
