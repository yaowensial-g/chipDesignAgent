"""SQLite 基础访问层。

项目目前是 Windows 本地单用户应用，因此使用零部署 SQLite。这个类只提供
最小的 execute/fetch/JSON 辅助，不在这里放业务规则；业务状态转换仍由 Service
负责。每次操作使用短连接，并通过进程内锁降低多线程 ToolRun 的写冲突。
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


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

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """提供跨多条关联写操作的原子事务，同时复用现有进程锁和 WAL。"""
        with self._lock, self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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
        -- Conversation 只保存会话索引信息；正文放在 conversation_messages，避免
        -- 列表页为了展示标题而扫描大段历史消息。
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            module_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '新会话',
            user_role TEXT NOT NULL,
            primary_agent TEXT NOT NULL,
            last_message_preview TEXT NOT NULL DEFAULT '',
            message_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- 聊天消息是后端事实源。sequence 是会话内稳定游标，前端刷新后可分页恢复。
        CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            turn_id TEXT,
            role TEXT NOT NULL,
            agent_role TEXT,
            content TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'chat',
            run_id TEXT,
            delegation_task_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES conversations(session_id)
        );
        -- 摘要只覆盖较早消息。covered_until_sequence 让 ContextBuilder 明确知道摘要
        -- 覆盖边界，避免摘要和近期原文重复进入 Prompt。
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            session_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            covered_until_sequence INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES conversations(session_id)
        );
        -- v2 摘要按角色隔离。旧表保留用于审计，但不再直接注入模型上下文；无法
        -- 可靠判断角色的旧摘要会由各角色从原始消息安全重建。
        CREATE TABLE IF NOT EXISTS conversation_role_summaries (
            session_id TEXT NOT NULL,
            agent_role TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            summary_text TEXT NOT NULL DEFAULT '',
            covered_from_sequence INTEGER NOT NULL DEFAULT 0,
            covered_until_sequence INTEGER NOT NULL DEFAULT 0,
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            summary_version INTEGER NOT NULL DEFAULT 1,
            generation_mode TEXT NOT NULL DEFAULT 'deterministic_extract',
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(session_id, agent_role),
            FOREIGN KEY(session_id) REFERENCES conversations(session_id)
        );
        -- 新摘要按完整问答轮次独立保存。旧的 conversation_role_summaries 暂时保留，
        -- 但 ContextBuilder 不再读取或同步更新它。
        CREATE TABLE IF NOT EXISTS conversation_turn_summaries (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            agent_role TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            user_message_id TEXT NOT NULL,
            assistant_message_id TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            token_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            generation_mode TEXT NOT NULL DEFAULT 'model_structured',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES conversations(session_id),
            FOREIGN KEY(user_message_id) REFERENCES conversation_messages(message_id),
            FOREIGN KEY(assistant_message_id) REFERENCES conversation_messages(message_id)
        );
        -- 原始文档、版本和检索分块分表保存。新版本不会覆盖旧版本，旧版本仅被标记
        -- superseded，便于追踪“某条结论当时依据的是哪版 AS”。
        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            module_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_versions (
            version_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(document_id)
        );
        CREATE TABLE IF NOT EXISTS document_chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            section TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            terms_json TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(document_id),
            FOREIGN KEY(version_id) REFERENCES document_versions(version_id)
        );
        -- 长期记忆和来源分离：一条记忆可由多个规格段落、问题单或用户决策共同支撑。
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            module_name TEXT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            authority TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_sources (
            source_ref_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_version TEXT,
            location TEXT,
            content_hash TEXT,
            source_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
        );
        CREATE TABLE IF NOT EXISTS memory_relations (
            relation_id TEXT PRIMARY KEY,
            from_memory_id TEXT NOT NULL,
            to_memory_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(from_memory_id) REFERENCES memories(memory_id),
            FOREIGN KEY(to_memory_id) REFERENCES memories(memory_id)
        );
        CREATE TABLE IF NOT EXISTS memory_extraction_events (
            extraction_key TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            module_name TEXT,
            agent_role TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            memory_ids_json TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- Context Manifest 记录每次模型调用实际选中了哪些消息、记忆和文档分块。
        -- 它是调试“模型为什么没看到某条规格”的关键审计对象。
        CREATE TABLE IF NOT EXISTS context_manifests (
            context_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            run_id TEXT,
            agent_role TEXT NOT NULL,
            token_budget INTEGER NOT NULL,
            estimated_tokens INTEGER NOT NULL,
            message_ids_json TEXT NOT NULL,
            memory_ids_json TEXT NOT NULL,
            chunk_ids_json TEXT NOT NULL,
            observation_ids_json TEXT NOT NULL,
            turn_summary_ids_json TEXT NOT NULL DEFAULT '[]',
            raw_turn_ids_json TEXT NOT NULL DEFAULT '[]',
            sections_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES conversations(session_id)
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_message_order
            ON conversation_messages(session_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_conversation_project
            ON conversations(project_id, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_document_identity
            ON documents(project_id, module_name, kind, name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_document_version_hash
            ON document_versions(document_id, version, content_hash);
        CREATE INDEX IF NOT EXISTS idx_document_chunks_active
            ON document_chunks(document_id, status, ordinal);
        CREATE INDEX IF NOT EXISTS idx_memories_scope
            ON memories(project_id, module_name, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_role_summaries_lookup
            ON conversation_role_summaries(session_id, agent_role, covered_until_sequence);
        CREATE INDEX IF NOT EXISTS idx_turn_summaries_lookup
            ON conversation_turn_summaries(session_id, agent_role, status, sequence);
        CREATE INDEX IF NOT EXISTS idx_memory_sources_source
            ON memory_sources(source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_memory_extractions_source
            ON memory_extraction_events(source_type, source_id, status);
        CREATE INDEX IF NOT EXISTS idx_context_run
            ON context_manifests(run_id, created_at);
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
            conversation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(conversations)")
            }
            conversation_migrations = {
                "module_name": "TEXT NOT NULL DEFAULT ''",
                "title": "TEXT NOT NULL DEFAULT '新会话'",
                "last_message_preview": "TEXT NOT NULL DEFAULT ''",
                "message_count": "INTEGER NOT NULL DEFAULT 0",
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "total_tokens": "INTEGER NOT NULL DEFAULT 0",
                "model_calls": "INTEGER NOT NULL DEFAULT 0",
                "active_run_id": "TEXT",
            }
            for column, declaration in conversation_migrations.items():
                if column not in conversation_columns:
                    connection.execute(
                        f"ALTER TABLE conversations ADD COLUMN {column} {declaration}"
                    )

            message_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(conversation_messages)")
            }
            if "turn_id" not in message_columns:
                connection.execute("ALTER TABLE conversation_messages ADD COLUMN turn_id TEXT")

            memory_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(memories)")
            }
            memory_migrations = {
                "memory_key": "TEXT",
                "importance": "INTEGER NOT NULL DEFAULT 5",
                "visibility": "TEXT NOT NULL DEFAULT 'project_shared'",
                "owner_agent_role": "TEXT",
                "review_reason": "TEXT",
                "extraction_key": "TEXT",
            }
            for column, declaration in memory_migrations.items():
                if column not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE memories ADD COLUMN {column} {declaration}"
                    )

            source_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(memory_sources)")
            }
            source_migrations = {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "invalidated_at": "TEXT",
                "invalidation_reason": "TEXT",
            }
            for column, declaration in source_migrations.items():
                if column not in source_columns:
                    connection.execute(
                        f"ALTER TABLE memory_sources ADD COLUMN {column} {declaration}"
                    )

            manifest_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(context_manifests)")
            }
            manifest_migrations = {
                "summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "excluded_json": "TEXT NOT NULL DEFAULT '[]'",
                "prompt_hash": "TEXT",
                "context_version": "INTEGER NOT NULL DEFAULT 2",
                "turn_summary_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "raw_turn_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, declaration in manifest_migrations.items():
                if column not in manifest_columns:
                    connection.execute(
                        f"ALTER TABLE context_manifests ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(project_id, module_name, memory_key, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_visibility ON memories(project_id, visibility, owner_agent_role, status)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_extraction_key ON memories(extraction_key) WHERE extraction_key IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_turn ON conversation_messages(session_id, turn_id, sequence)"
            )
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
