"""从已完成的 Run 读取对话与有限的工程证据。"""

from collections.abc import Callable
from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from app.domain.models import MemoryView
from app.services.config_service import ConfigService, config_context
from app.services.memory_service import MemoryService
from app.services.session_service import SessionService
from app.services.token_service import TokenCounter
from app.storage.orchestration_repository import OrchestrationRepository


# 读取接口接收项目、模块和本轮请求，返回已按本轮相关性投影的配置（含已有版本信息）。
ProjectConfigReader = Callable[[str, str, str], dict[str, Any] | None]


@dataclass(frozen=True)
class MemoryExtractionContext:
    run_id: str
    project_id: str
    module_name: str
    user_request: str
    assistant_response: str
    run_summary: dict[str, list[dict]]
    project_config: dict | None
    related_memories: dict[str, MemoryView]
    configured_facts: frozenset[str] = frozenset()
    message_ids: tuple[str, ...] = ()
    session_id: str | None = None
    agent_role: str | None = None

    def repeats_config(self, content: str) -> bool:
        return _fact_text(content) in self.configured_facts

class MemoryExtractionContextBuilder:
    def __init__(
        self, sessions: SessionService, memories: MemoryService, tokens: TokenCounter,
        read_project_config: ProjectConfigReader | None,
        configs: ConfigService | None = None,
    ) -> None:
        self.sessions = sessions
        self.db = sessions.db
        self.memories = memories
        self.tokens = tokens
        self.read_project_config = read_project_config
        self.configs = configs
        self.repo = OrchestrationRepository(self.db, sessions)

    def build(self, run: dict) -> MemoryExtractionContext:
        run_id, role = run["run_id"], run["primary_agent"]
        project_id, module_name = run["project_id"], run["module_name"]
        messages = self.sessions.run_messages(run["session_id"], run_id, role)
        root = next((item for item in messages if item.role == "user" and item.message_type == "chat"), None)
        final = next((item for item in reversed(messages) if item.role == "assistant"), None)
        if final is None:
            raise ValueError("completed Run is missing its persisted assistant response")
        user_request = root.content if root else run["user_instruction"]
        follow_ups = [
            {"message_id": item.message_id, "role": item.role, "content": item.content,
             **({"action": item.metadata["action"]} if "action" in item.metadata else {})}
            for item in messages if item != root and item != final
        ]
        result = self.db.loads(run["result_json"], {})
        summary = {
            "decisions": follow_ups,
            "observations": [],
            "tool_results": [],
            "delegation_results": self._delegations(run_id, role),
            "artifacts": [],
        }
        if run["engine_version"] == "hierarchical":
            self._hierarchical_evidence(run_id, summary)
        self._result_evidence(run, result, summary)

        query = "\n".join([user_request, *(item["content"] for item in follow_ups if item["role"] == "user")])
        related = self.memories.retrieve(project_id, module_name, query, agent_role=role)
        pinned = self.configs.run_config(run_id, final=True) if self.configs else None
        if pinned is not None and pinned.project_id != project_id:
            raise ValueError("Memory extraction configuration project mismatch")
        config = (
            config_context(pinned, module_name, query, self.tokens, 1_500)[0] if pinned is not None
            else self._bounded_config(self.read_project_config(project_id, module_name, query))
            if self.read_project_config else None
        )
        facts: set[str] = set()
        if pinned is not None:
            for name, module in pinned.scope(module_name).modules.items():
                for instance, parameter, item in module.items():
                    keys = [f"{instance}.{parameter}" if instance else parameter]
                    keys.append(f"{name}.{keys[0]}")
                    value = item.value if isinstance(item.value, str) else self.db.dumps(item.value)
                    facts.add(_fact_text(item.source_evidence))
                    for key in keys:
                        for separator in ("=", ":", " is ", " 为 ", " 设为 "):
                            facts.add(_fact_text(f"{key}{separator}{value}"))
        return MemoryExtractionContext(
            run_id, project_id, module_name, user_request, final.content, summary,
            config,
            {item.memory_id: item for item in related if self.tokens.count(item.content) <= 500},
            frozenset(facts),
            tuple(item.message_id for item in messages), run["session_id"], role,
        )

    def _delegations(self, run_id: str, role: str) -> list[dict]:
        rows = self.db.fetch_all(
            """SELECT task_id, to_agent, result_json FROM delegation_tasks
            WHERE parent_run_id = ? AND from_agent = ? AND status = 'completed'
            ORDER BY created_at LIMIT 3""",
            (run_id, role),
        )
        results = []
        for row in rows:
            result = self.db.loads(row["result_json"], {})
            results.append({
                "task_id": row["task_id"], "expert": row["to_agent"],
                "suggestion": self.tokens.trim(result.get("conclusion", ""), 200),
                "references": [self.tokens.trim(ref, 40) for ref in result.get("evidence", [])[:4]],
            })
        return results

    def _hierarchical_evidence(self, run_id: str, summary: dict[str, list[dict]]) -> None:
        for state in self.repo.states(run_id).values():
            if state.status != "completed":
                continue
            for observation in self.repo.observations(state.task_id, state.generation):
                refs = [
                    {"kind": ref.kind, "ref_id": ref.ref_id} for ref in observation.evidence
                    if ref.kind in {"tool_run", "artifact"}
                ]
                if refs and len(summary["observations"]) < 8:
                    summary["observations"].append({
                        "observation_id": observation.observation_id, "tool_name": observation.tool_name,
                        "status": observation.status, "summary": self.tokens.trim(observation.summary, 160),
                        "evidence": refs[:4],
                    })
            remaining_checks = 8 - len(summary["tool_results"])
            if remaining_checks > 0:
                recent_checks = self.repo.verifications(state.task_id, state.generation)[-min(2, remaining_checks):]
                for check in recent_checks:
                    summary["tool_results"].append({
                        "verification_id": check["verification_id"], "scope": check["scope"],
                        "passed": check["result"]["passed"], "artifact_ids": check["artifact_ids"][:4],
                        "failed_criteria": check["result"].get("failed_criteria", [])[:4],
                    })

        rows = self.db.fetch_all(
            """SELECT a.artifact_id, a.version, json_extract(a.artifact_json, '$.name') AS name,
            json_extract(a.artifact_json, '$.artifact_type') AS artifact_type
            FROM orchestration_artifacts a JOIN orchestration_tasks t USING(task_id)
            WHERE t.run_id = ? AND a.status = 'verified'
              AND json_extract(a.artifact_json, '$.source_generation') = json_extract(t.state_json, '$.generation')
            ORDER BY a.rowid DESC LIMIT 8""",
            (run_id,),
        )
        summary["artifacts"].extend(rows)

    def _result_evidence(self, run: dict, result: dict, summary: dict[str, list[dict]]) -> None:
        if result.get("mock"):
            return
        # 读取真实 ToolRun 的结构化状态，不搬运 stdout/stderr、命令行和原始输出。
        tool_ids = [
            item.get("tool_run_id") for item in result.get("eda_tool_runs", [])[:8]
        ] + [run["tool_run_id"], result.get("lint_tool_run_id")]
        for tool_id in dict.fromkeys(filter(None, tool_ids)):
            row = self.db.fetch_one(
                """SELECT t.run_id AS tool_run_id, t.tool_name, t.status, t.exit_code,
                   json_extract(e.result_json, '$.status') AS verification_status,
                   json_extract(e.result_json, '$.metrics') AS metrics_json
                FROM tool_runs t LEFT JOIN eda_tool_results e ON e.tool_run_id = t.run_id
                WHERE t.run_id = ? AND t.status IN ('completed', 'failed', 'timed_out')""",
                (tool_id,),
            )
            if row:
                metrics = self.db.loads(row.pop("metrics_json"), {})
                row["metrics"] = dict([
                    (key, value) for key, value in metrics.items() if isinstance(value, (bool, int, float))
                ][:12])
                summary["tool_results"].append(row)
        for kind, id_key in (("patch", "patch_id"), ("task", "task_id")):
            artifact = result.get(kind) or {}
            if artifact.get(id_key):
                summary["artifacts"].append({
                    "artifact_id": artifact[id_key], "artifact_type": kind, "status": artifact.get("status"),
                })

    def _bounded_config(self, config: dict | None) -> dict | None:
        if config is None:
            return None
        selected: dict[str, Any] = {}
        for key in dict.fromkeys([key for key in ("revision", "version") if key in config] + list(config)):
            candidate = {**selected, key: config[key]}
            if self.tokens.count(self.db.dumps(candidate)) <= 1_500:
                selected = candidate
        return selected


def _fact_text(content: str) -> str:
    text = unicodedata.normalize("NFKC", content).casefold().strip().rstrip(".。!！")
    return re.sub(r"[\s_]+", "", text)
