"""SQLite 基础访问层。

项目目前是 Windows 本地单用户应用，因此使用零部署 SQLite。这个类只提供
最小的 execute/fetch/JSON 辅助，不在这里放业务规则；业务状态转换仍由 Service
负责。每次操作使用短连接，并通过进程内锁降低多线程 ToolRun 的写冲突。
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "chipflow.db"


class Database:
    """负责建表、兼容迁移和线程安全的基础 SQL 访问。"""
    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        # 测试可以注入临时数据库路径；正常运行默认写入 data/chipflow.db。
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        """创建短生命周期连接，并统一启用外键与 WAL。"""
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        """执行一条写 SQL 并立即提交事务。参数必须使用占位符，禁止字符串拼接。"""
        with self._lock, self.connect() as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """查询单行并转换成普通字典，避免 Service 依赖 sqlite3.Row。"""
        with self._lock, self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """查询多行并转换为字典列表。"""
        with self._lock, self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def _initialize(self) -> None:
        """幂等创建当前 Schema，并为旧数据库补充新增列。

        MVP 尚未引入 Alembic，所以只允许在这里做简单、向后兼容的 ADD COLUMN。
        复杂生产迁移应改用正式迁移工具，不能继续堆叠手写条件分支。
        """
        schema = """
        -- Workspace 是所有文件操作的安全根目录，禁止 Tool 任意访问用户磁盘。
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
        -- Run 是用户任务控制面；专业执行细节通过下方 ID 关联，不全部复制进来。
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
            primary_agent TEXT,
            legacy_task_id TEXT,
            tool_run_id TEXT,
            patch_id TEXT,
            pending_user_request_json TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- Event 使用 run_id + sequence 保证同一 Run 内可按稳定顺序回放。
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
        -- Conversation 保存工作角色和主 Agent，不保存整段聊天正文。
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            user_role TEXT NOT NULL,
            primary_agent TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- 每次跨专业 Delegate 都有独立记录，失败或超时也不会丢失审计线索。
        CREATE TABLE IF NOT EXISTS delegation_tasks (
            task_id TEXT PRIMARY KEY,
            parent_run_id TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            task_type TEXT NOT NULL,
            instruction TEXT NOT NULL,
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            expected_output TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            requires_human_intervention INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_issues_run_order ON engineering_issues(run_id, queue_index);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_issue_fingerprint ON engineering_issues(run_id, fingerprint);
        CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_order ON run_events(run_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_delegations_run ON delegation_tasks(parent_run_id, created_at);
        """
        with self._lock, self.connect() as connection:
            connection.executescript(schema)
            # 兼容已有本地数据库：CREATE TABLE IF NOT EXISTS 不会为旧表增加新列，
            # 因而必须检查 PRAGMA 后再执行最小迁移。
            issue_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(engineering_issues)")
            }
            if "analysis_json" not in issue_columns:
                connection.execute(
                    "ALTER TABLE engineering_issues ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'"
                )
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            if "primary_agent" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN primary_agent TEXT")
            connection.commit()

    @staticmethod
    def dumps(value: Any) -> str:
        """以 UTF-8 友好的紧凑格式序列化 JSON，保留中文可读性。"""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def loads(value: str | None, default: Any) -> Any:
        """反序列化可空 JSON 字段；空值返回调用方提供的默认值。"""
        if not value:
            return default
        return json.loads(value)


database = Database()
