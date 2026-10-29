"""主 Agent 与专家 Agent 之间的受控委派服务。

Delegate 不是把整个会话交给另一个 Agent，也不是简单地再调用一次 Prompt。
每次委派都有独立任务 ID、父 Run、输入制品、约束、状态和结构化结果。
主 Agent 始终保留会话控制权，专家结论只作为 Observation 返回。
"""

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


# 防止主 Agent 反复委派造成循环、Token 失控或“为了闭环而讨论”。
MAX_DELEGATIONS_PER_RUN = 3

# 模型或网络故障不能永久占住一次用户请求。超时后将任务标为 timed_out，
# 主 Agent 可以把失败状态反馈给用户，而不是无限等待。
DELEGATION_TIMEOUT_SECONDS = 90

# 白名单表示“谁可以向谁求助”。Architecture 只作为专家存在，不能作为发起者，
# 因而天然禁止专家继续嵌套委派。
ALLOWED_EXPERTS = {
    AgentRole.DESIGN: {AgentRole.VERIFICATION, AgentRole.ARCHITECTURE},
    AgentRole.VERIFICATION: {AgentRole.DESIGN, AgentRole.ARCHITECTURE},
}


class DelegationService:
    """在主 Agent 保留控制权的前提下执行有边界的专家任务。"""

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
        """创建、执行并持久化一次专家委派。

        执行顺序不能随意交换：先校验专家白名单和次数，再落库 queued/running，
        最后调用专家。这样即使模型调用失败，数据库中仍能看到失败发生在哪一步。
        """
        # 不能因为主 Agent 在 Prompt 中声称“需要某专家”，就跳过后端白名单。
        if to_agent not in ALLOWED_EXPERTS.get(from_agent, set()):
            raise PermissionError(
                f"{from_agent.value} cannot delegate to {to_agent.value}"
            )
        # 次数统计读取持久化记录，而不是进程内计数；服务重启后限制仍然有效。
        count = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM delegation_tasks WHERE parent_run_id = ?",
            (parent_run_id,),
        )
        if int(count["count"] or 0) >= MAX_DELEGATIONS_PER_RUN:
            raise ValueError("Delegation limit reached for this Run")

        # 只保存文档名称和版本作为来源引用，不在 delegation_tasks 表中复制整篇文档。
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
            # 当前专家只能分析，不能写正式文件，也不能再次发起 Delegate。
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

        # 专家只得到完成任务所需的最小上下文。完整聊天历史可能含有主 Agent
        # 的私有推理或无关信息，因此在 _scoped_context 中明确置空。
        scoped = self._scoped_context(request_payload, delegation)
        try:
            # Runtime 当前是同步调用；to_thread 避免它阻塞 FastAPI 事件循环，
            # wait_for 则给这次委派增加明确的时间边界。
            analysis = await asyncio.wait_for(
                asyncio.to_thread(self._expert_callable(to_agent), scoped),
                timeout=DELEGATION_TIMEOUT_SECONDS,
            )
            conclusion = str(analysis.get("summary", "专家分析已完成。"))
            evidence = self._evidence(analysis, artifacts)
            # 统一成稳定结果契约。未来专家换模型或拆成 Worker，调用方仍只依赖这些字段。
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
        """把数据库行和 JSON 字段还原为强类型 ``DelegationResult``。"""
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
        """按创建顺序查询某个父 Run 的全部专家任务。"""
        rows = self.db.fetch_all(
            "SELECT task_id FROM delegation_tasks WHERE parent_run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [self.get(row["task_id"]) for row in rows]

    @staticmethod
    def _scoped_context(
        request_payload: dict[str, Any], delegation: DelegationRequest
    ) -> dict[str, Any]:
        """构造专家最小上下文，避免无边界复制主 Agent 私有历史。"""
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
        """把专家角色映射到 Runtime 方法；未知角色会直接抛出 KeyError。"""
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
        """合并请求制品引用与模型返回来源，并保持首次出现顺序去重。"""
        evidence = [f"{item.path}@{item.version}" for item in artifacts]
        for source in analysis.get("sources", []):
            if isinstance(source, dict):
                evidence.append(
                    f"{source.get('document', 'document')}@{source.get('version', '')}"
                )
        return list(dict.fromkeys(evidence))

    def _set_status(self, task_id: str, status: DelegationStatus) -> None:
        """更新正常状态和时间戳。"""
        self.db.execute(
            "UPDATE delegation_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status.value, utc_now(), task_id),
        )

    def _fail(self, task_id: str, status: DelegationStatus, error: str) -> None:
        """记录失败，并显式标记需要主 Agent 或用户介入。"""
        self.db.execute(
            """UPDATE delegation_tasks SET status = ?, error = ?,
            requires_human_intervention = 1, updated_at = ? WHERE task_id = ?""",
            (status.value, error, utc_now(), task_id),
        )


delegation_service = DelegationService()
