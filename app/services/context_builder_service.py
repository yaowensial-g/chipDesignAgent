"""按 Token 预算组装模型上下文，并记录可审计的 Context Manifest。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.domain.context import ContextBuildRequest, ContextPurpose as Purpose, InvocationContext, RunBaseContext
from app.domain.design_config import ProjectDesignConfig, normalize_module
from app.domain.orchestration import ArtifactPayload, Observation, RunExecutionPolicy, TaskContract, TaskPlan, TaskState

from app.domain.models import (
    ChatHistoryMessage,
    ChatRequest,
    ContextManifestView,
    DelegationRequest,
    DocumentIngestRequest,
    DocumentInput,
    RetrievalRequest,
    RunView,
)
from app.config.model_settings import model_settings_service
from app.models.provider import ModelInvocationError
from app.services.config_service import config_context
from app.services.eda_workflow import eda_workflow
from app.services.task_verifier_service import TaskVerifierService
from app.services.context_profiles import CONTEXT_PROFILES
from app.services.document_service import DocumentService, document_service
from app.services.conversation_summary_service import (
    ConversationSummaryService,
    conversation_summary_service,
)
from app.services.memory_service import MemoryService, memory_service
from app.services.session_service import SessionService, session_service
from app.services.token_service import TokenCounter, token_counter
from app.services.workspace_service import utc_now
from app.storage.database import Database, database
from app.storage.orchestration_repository import OrchestrationRepository
from app.tools.project_manifest import FrozenProject


class ContextBudgetExceeded(ModelInvocationError):
    """Required model input exceeds its configured context window."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="MODEL_CONTEXT_BUDGET_EXCEEDED", http_status=400)


_PROBE = "只回答 CHIP_AGENT_CONNECTION_OK"
_JSON_PREFIX = "请只返回合法 JSON，不要使用 Markdown 代码块。输入如下：\n"
_CONFIG_GUIDANCE = (
    "项目配置是当前目标规格，优先于长期记忆。pending 表示尚无实现证据。"
    "如果配置与 RTL 或验证结果冲突，指出差异并依据工程证据处理。"
)


@dataclass
class ContextBuildResult:
    """一次上下文选择结果；effective_request 才会被发送给模型。"""

    effective_request: ChatRequest
    message_ids: list[str]
    turn_summary_ids: list[str]
    raw_turn_ids: list[str]
    memory_ids: list[str]
    chunk_ids: list[str]
    memory_context: list[dict]
    project_config: dict
    estimated_tokens: int
    sections: dict[str, object]
    summary: dict[str, object]
    excluded: list[dict[str, object]]
    prompt_hash: str


@dataclass(frozen=True)
class ContextBudgetConfig:
    total: int = 32_000
    system_and_tools: int = 4_000
    current_request_and_run: int = 3_000
    recent_messages: int = 6_000
    summary: int = 6_000
    long_term_memory: int = 2_000
    documents: int = 12_000
    output_reserve: int = 3_000
    project_config: int = 2_500


class ContextBuilderService:
    """把短期历史、摘要、长期记忆和 RAG 片段组合成有界上下文。"""

    def __init__(
        self,
        db: Database = database,
        sessions: SessionService = session_service,
        documents: DocumentService = document_service,
        memories: MemoryService = memory_service,
        summaries: ConversationSummaryService = conversation_summary_service,
        tokens: TokenCounter = token_counter,
        token_budget: int = 32_000,
        budget_config: ContextBudgetConfig | None = None,
    ) -> None:
        self.db = db
        self.sessions = sessions
        self.documents = documents
        self.memories = memories
        self.summaries = summaries
        self.tokens = tokens
        self.budget = budget_config or ContextBudgetConfig(total=token_budget)
        self._uses_default_budget = budget_config is None
        self.token_budget = self.budget.total

    def build(
        self,
        session_id: str,
        request: ChatRequest,
        agent_role: str,
        current_message_id: str,
        *,
        before_sequence: int | None = None,
        pinned_version_ids: tuple[str, ...] = (),
        project_config: ProjectDesignConfig | None = None,
    ) -> ContextBuildResult:
        # 首次上传仍兼容旧前端：入库后下轮只需项目/模块和查询，不必再次传整篇正文。
        for document in request.documents:
            self.documents.ingest(
                DocumentIngestRequest(
                    project_id=request.project_id,
                    module_name=request.module_name,
                    kind=document.kind,
                    name=document.name,
                    version=document.version,
                    content=document.content,
                )
            )

        # 对话历史以完整问答轮次为原子单位。当前 User 尚无最终 Assistant，天然不会
        # 出现在 completed_turns 中；exclude_message_id 再提供一道防重复保护。
        completed_turns = self.sessions.completed_turns(
            session_id,
            agent_role=agent_role,
            exclude_message_id=current_message_id,
            before_sequence=before_sequence,
        )
        recent_limit = self.summaries.policy.recent_turns
        older_limit = min(self.summaries.policy.history_turns,
                          CONTEXT_PROFILES[Purpose.CHAT_BASE].max_history_turns - recent_limit)
        recent_turns = completed_turns[-recent_limit:]
        older_turns = completed_turns[:-recent_limit][-older_limit:] if older_limit > 0 else []
        oldest_recent_sequence = recent_turns[0].sequence if recent_turns else None
        completed_summaries = self.summaries.get_recent_turn_summaries(
            session_id,
            agent_role,
            before_sequence=oldest_recent_sequence,
            limit=self.summaries.policy.history_turns,
        )
        summaries_by_turn = {item.turn_id: item for item in completed_summaries}

        query_tokens = self.tokens.count(request.message)
        configured_output = model_settings_service.effective().max_output_tokens
        output_reserve = (
            max(self.budget.output_reserve, min(configured_output, 8_000))
            if self._uses_default_budget
            else self.budget.output_reserve
        )
        fixed_tokens = (
            self.budget.system_and_tools
            + max(self.budget.current_request_and_run, query_tokens)
            + output_reserve
        )
        if fixed_tokens > self.token_budget:
            raise ValueError(
                f"当前请求与强制保留区需要 {fixed_tokens} tokens，超过上下文预算 {self.token_budget}"
            )
        remaining = self.token_budget - fixed_tokens
        excluded: list[dict[str, object]] = []
        config, omitted_config_items = {}, 0
        if project_config is not None:
            if project_config.project_id != request.project_id:
                raise ValueError("Project configuration scope mismatch")
            config, omitted_config_items = config_context(
                project_config, request.module_name, request.message, self.tokens,
                min(self.budget.project_config, remaining),
            )
        config_tokens = self.tokens.count(self.db.dumps(config)) if config else 0
        remaining -= config_tokens

        history: list[ChatHistoryMessage] = []
        selected_messages = []
        selected_raw_turns = []
        selected_summary_rows = []

        # 最近轮次优先占用约 6000 Token，User/Assistant 两条消息要么同时入选，
        # 要么整轮排除，绝不会留下半轮上下文。
        recent_cap = min(self.budget.recent_messages, remaining)
        recent_used = 0
        for turn in reversed(recent_turns):
            cost = self.tokens.count(turn.user.content) + self.tokens.count(turn.assistant.content)
            if recent_used + cost > recent_cap:
                excluded.append({
                    "category": "raw_turn",
                    "id": turn.turn_id,
                    "reason": "近期完整问答区预算不足；整轮排除",
                    "tokens": cost,
                })
                continue
            selected_raw_turns.append(turn)
            recent_used += cost
        selected_raw_turns.reverse()
        remaining -= recent_used

        # 最近 5 轮之前只看最近 30 个完整轮次。已完成摘要优先使用；pending、
        # processing、failed 或尚未排队的轮次回退到原始问答，避免异步延迟丢信息。
        older_units = []
        for turn in older_turns:
            summary_row = summaries_by_turn.get(turn.turn_id)
            if summary_row:
                text = (
                    f"[独立轮次摘要 turn_id={turn.turn_id}; sequence={turn.sequence}; "
                    f"role={agent_role}]\n"
                    + json.dumps(summary_row.summary_json, ensure_ascii=False)
                )
                older_units.append((turn, summary_row, text, self.tokens.count(text)))
            else:
                cost = self.tokens.count(turn.user.content) + self.tokens.count(turn.assistant.content)
                older_units.append((turn, None, None, cost))

        older_cap = min(self.budget.summary, remaining)
        older_used = 0
        selected_older = []
        for unit in reversed(older_units):
            turn, summary_row, _, cost = unit
            if older_used + cost > older_cap:
                excluded.append({
                    "category": "turn_summary" if summary_row else "raw_turn_fallback",
                    "id": summary_row.id if summary_row else turn.turn_id,
                    "reason": "较早轮次区预算不足；摘要或完整回退轮次整体排除",
                    "tokens": cost,
                })
                continue
            selected_older.append(unit)
            older_used += cost
        selected_older.reverse()
        remaining -= older_used

        for turn, summary_row, text, _ in selected_older:
            if summary_row:
                selected_summary_rows.append(summary_row)
                history.append(ChatHistoryMessage(role="assistant", content=text))
            else:
                selected_messages.extend((turn.user, turn.assistant))
                history.extend((
                    ChatHistoryMessage(role="user", content=turn.user.content),
                    ChatHistoryMessage(role="assistant", content=turn.assistant.content),
                ))
        for turn in selected_raw_turns:
            selected_messages.extend((turn.user, turn.assistant))
            history.extend((
                ChatHistoryMessage(role="user", content=turn.user.content),
                ChatHistoryMessage(role="assistant", content=turn.assistant.content),
            ))

        history_tokens = recent_used + older_used
        summary_tokens = sum(unit[3] for unit in selected_older if unit[1] is not None)
        raw_turn_ids = [unit[0].turn_id for unit in selected_older if unit[1] is None]
        raw_turn_ids.extend(turn.turn_id for turn in selected_raw_turns)
        turn_summary_ids = [item.id for item in selected_summary_rows]
        # 新会话迁移期若后端还没有历史，则使用旧客户端携带的历史作为一次性回退。
        if not history and request.history:
            for item in reversed(request.history[-12:]):
                cost = self.tokens.count(item.content)
                if cost <= remaining:
                    history.insert(0, item)
                    history_tokens += cost
                    remaining -= cost

        memories = self.memories.retrieve(
            request.project_id,
            request.module_name,
            request.message,
            agent_role=agent_role,
        )
        selected_memories = []
        memory_tokens = 0
        memory_cap = min(self.budget.long_term_memory, remaining)
        for memory in memories:
            cost = self.tokens.count(memory.content)
            critical = memory.selection_reason == "critical_constraint"
            allowed = remaining if critical else memory_cap
            if memory_tokens + cost <= allowed:
                selected_memories.append(memory)
                memory_tokens += cost
            else:
                excluded.append({
                    "category": "memory",
                    "id": memory.memory_id,
                    "reason": "长期记忆区预算不足",
                    "tokens": cost,
                    "score": memory.retrieval_score,
                })
        remaining -= memory_tokens
        memory_context = [
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind,
                "content": memory.content,
            }
            for memory in selected_memories
        ]

        retrieval = self.documents.retrieve(
            RetrievalRequest(
                project_id=request.project_id,
                module_name=request.module_name,
                query=request.message,
                top_k=50,
            ),
            pinned_version_ids=pinned_version_ids,
        )
        selected_chunks = []
        document_tokens = 0
        # 文档是最后一个大区段，可使用前面摘要/历史/记忆未消耗的额度。
        document_cap = remaining
        current_document_refs = {(item.name, item.version) for item in request.documents}
        for chunk in retrieval.chunks:
            current_request_evidence = chunk.version_id in pinned_version_ids or (
                chunk.document_name, chunk.document_version
            ) in current_document_refs
            if chunk.score <= 0 and not current_request_evidence:
                excluded.append({
                    "category": "document_chunk",
                    "id": chunk.chunk_id,
                    "reason": "检索相关性为零",
                    "score": chunk.score,
                })
                continue
            if document_tokens + chunk.token_count > document_cap:
                excluded.append({
                    "category": "document_chunk",
                    "id": chunk.chunk_id,
                    "reason": "文档证据区预算不足",
                    "tokens": chunk.token_count,
                    "score": chunk.score,
                })
                continue
            selected_chunks.append(chunk)
            document_tokens += chunk.token_count
        remaining -= document_tokens
        documents = [
            DocumentInput(
                kind=chunk.document_kind,
                name=chunk.document_name,
                version=chunk.document_version,
                content=(
                    f"[RAG source: chunk={chunk.chunk_id}; section={chunk.section}; "
                    f"score={chunk.score}]\n{chunk.content}"
                ),
            )
            for chunk in selected_chunks
        ]

        estimated = fixed_tokens + config_tokens + history_tokens + memory_tokens + document_tokens
        if estimated > self.token_budget:
            raise RuntimeError("ContextBuilder produced an over-budget context")
        effective = request.model_copy(update={"history": history, "documents": documents})
        sections = {
            "project_config": {"revision": config.get("revision"), "tokens": config_tokens,
                               "omitted_items": omitted_config_items},
            "history": {
                "items": len(history),
                "tokens": history_tokens,
                "raw_turns": len(raw_turn_ids),
                "turn_summaries": len(turn_summary_ids),
            },
            "summary": {
                "items": len(selected_summary_rows),
                "tokens": summary_tokens,
                "turn_summary_ids": turn_summary_ids,
            },
            "raw_conversation": {
                "turns": len(raw_turn_ids),
                "messages": len(selected_messages),
                "tokens": history_tokens - summary_tokens,
                "turn_ids": raw_turn_ids,
            },
            "memory": {
                "items": len(selected_memories),
                "tokens": memory_tokens,
                "scores": [
                    {"memory_id": item.memory_id, "score": item.retrieval_score, "reason": item.selection_reason}
                    for item in selected_memories
                ],
            },
            "documents": {
                "items": len(selected_chunks),
                "tokens": document_tokens,
                "scores": [
                    {
                        "chunk_id": item.chunk_id,
                        "score": item.score,
                        "keyword_score": item.keyword_score,
                        "semantic_score": item.semantic_score,
                        "reason": (
                            "current_request_document"
                            if (item.document_name, item.document_version) in current_document_refs
                            else "hybrid_retrieval_rank"
                        ),
                    }
                    for item in selected_chunks
                ],
            },
            "query": {"tokens": query_tokens},
            "reserved": {
                "system_and_tools": self.budget.system_and_tools,
                "current_request_and_run": max(self.budget.current_request_and_run, query_tokens),
                "output": output_reserve,
            },
            "unused_tokens": self.token_budget - estimated,
        }
        prompt_hash = hashlib.sha256(json.dumps({
            "message": request.message,
            "message_ids": [message.message_id for message in selected_messages],
            "turn_summary_ids": turn_summary_ids,
            "raw_turn_ids": raw_turn_ids,
            "memory_ids": [memory.memory_id for memory in selected_memories],
            "project_config": config,
            "chunk_ids": [chunk.chunk_id for chunk in selected_chunks],
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return ContextBuildResult(
            effective_request=effective,
            message_ids=[message.message_id for message in selected_messages],
            turn_summary_ids=turn_summary_ids,
            raw_turn_ids=raw_turn_ids,
            memory_ids=[memory.memory_id for memory in selected_memories],
            chunk_ids=[chunk.chunk_id for chunk in selected_chunks],
            memory_context=memory_context,
            project_config=config,
            estimated_tokens=estimated,
            sections=sections,
            summary={
                "agent_role": agent_role,
                "turn_summary_ids": turn_summary_ids,
                "turn_ids": [item.turn_id for item in selected_summary_rows],
                "raw_turn_ids": raw_turn_ids,
            },
            excluded=excluded,
            prompt_hash=prompt_hash,
        )

    @staticmethod
    def run_snapshot(result: ContextBuildResult, manifest: ContextManifestView | None = None,
                     *, hierarchical: bool = False) -> dict:
        fields = {"history", "documents", "project_id", "module_name", "message"} if hierarchical else None
        return {
            **result.effective_request.model_dump(mode="json", include=fields),
            "_memory_context": result.memory_context,
            "_project_config": result.project_config,
            "_context_manifest": {
                "context_id": manifest.context_id if manifest else None,
                "message_ids": result.message_ids, "turn_summary_ids": result.turn_summary_ids,
                "raw_turn_ids": result.raw_turn_ids, "memory_ids": result.memory_ids, "chunk_ids": result.chunk_ids,
                "project_config": result.sections["project_config"],
            },
        }

    @staticmethod
    def run_base_context(checkpoint: dict) -> RunBaseContext:
        return RunBaseContext(checkpoint["context_id"], json.loads(checkpoint["snapshot"]))

    def replanning_inputs(self, repo: OrchestrationRepository, run: RunView, request: ChatRequest, plan: TaskPlan,
                          states: dict[str, TaskState], feedback: list[str]) -> dict:
        return {
            "raw_request": run.user_instruction, "module_name": request.module_name, "rtl_path": request.rtl_path,
            "clarifications": [item["content"] for item in repo.decisions(run.run_id) if item["action"] == "provide_resolution"],
            "validation_feedback": feedback,
            "tasks": [self._replan_task_context(repo, states[task.task_id]) for task in plan.tasks],
        }

    def _replan_task_context(self, repo: OrchestrationRepository, state: TaskState) -> dict:
        verification = repo.last_verification(state.task_id, state.generation)
        recent = CONTEXT_PROFILES[Purpose.REPLANNING].max_observations
        return {
            "task_id": state.task_id, "status": state.status, "failure_reason": state.failure_reason,
            "observations": [item.model_dump(mode="json")
                             for item in repo.observations(state.task_id, state.generation)[-recent:]],
            "bound_inputs": repo.input_bindings(state.task_id, state.generation),
            "verification_feedback": json.loads(verification["result_json"]) if verification else None,
            "verified_outputs": [item.artifact.model_dump(mode="json")
                                 for item in repo.local_outputs(state.task_id, "verified", state.generation)],
        }

    def reflection_inputs(
        self, repo: OrchestrationRepository, run: RunView, request: ChatRequest, plan: TaskPlan, contract: TaskContract, state: TaskState,
        observations: tuple[Observation, ...], inputs: tuple[ArtifactPayload, ...], candidates: tuple[ArtifactPayload, ...],
        verification: dict | None, limits: RunExecutionPolicy, replanning_enabled: bool, base_context: RunBaseContext,
    ) -> dict:
        actions, decisions = repo.usage(run.run_id)
        task_limits = contract.execution_policy
        tokens = self.tokens
        payload = {
            "message": contract.goal,
            "rtl_path": request.rtl_path if "read_rtl" in contract.capability_requirements else None,
            "observations": [
                {**item.model_dump(mode="json"), "summary": tokens.trim(item.summary, 180)}
                for item in observations
            ],
            "artifacts": [item.artifact.model_dump(mode="json") for item in (*inputs, *candidates)],
            "verification_feedback": json.loads(verification["result_json"]) if verification else None,
            "replanning_enabled": replanning_enabled,
            "plan_tasks": [{"task_id": task.task_id, "title": task.title} for task in plan.tasks],
            "budget": {
                "task": self._budget(
                    action=(state.action_count, task_limits.max_action_steps),
                    decision=(state.current_iteration, task_limits.max_decisions),
                    repair=(state.repair_attempts, task_limits.max_repair_attempts),
                ),
                "run": self._budget(
                    action=(actions, limits.max_run_actions), decision=(decisions, limits.max_run_decisions),
                    replan=(repo.replan_count(run.run_id), limits.max_replans),
                ),
            },
            "output_guidance": "报告在 submit_task_output.text_outputs 提交，引用每份分析和 Lint 结果为 [artifact:ID]。",
        }
        if state.generation > 1:
            previous_generation = state.generation - 1
            previous = repo.last_verification(state.task_id, previous_generation)
            attempt = next((item for item in reversed(repo.attempts(run.run_id, state.task_id))
                            if item["generation"] == previous_generation), {})
            feedback = repo.db.loads(previous["result_json"], {}) if previous else {}
            reason = attempt.get("failure_reason")
            if (feedback and not feedback.get("passed")) or reason:
                payload["previous_attempt_feedback"] = {
                    "failed_criteria": feedback.get("failed_criteria", ()),
                    "suggestions": feedback.get("suggestions", ()),
                    "failure_reason": tokens.trim(reason, 200) if reason else None,
                }
        if contract.assigned_agent == run.primary_agent:
            payload["user_request"] = run.user_instruction
            payload["clarifications"] = [item["content"] for item in repo.decisions(run.run_id)]
            if request.orchestration_mode == "chat":
                payload["conversation"] = base_context.snapshot
            else:
                payload["project_configuration"] = base_context.snapshot.get("_project_config", {})
        else:
            payload["clarifications"] = [item["content"] for item in repo.decisions(run.run_id, contract.task_id)]
        if "real_rtl_lint" in contract.capability_requirements:
            payload["verilator_lint_arguments"] = {
                "files": [request.rtl_path], "top_module": request.lint_top_module or request.module_name,
            }
        if request.orchestration_mode == "eda_project":
            payload["eda_arguments"] = eda_workflow(request, run.primary_agent)[1]
            payload["output_guidance"] = "提交 result。最终报告用 [artifact:ID] 引用每份证据，并照录实际 EDA 状态。"
            if "propose_eda_patch" in contract.capability_requirements:
                project = next(item for item in inputs if item.artifact.artifact_type == "project_manifest")
                files = FrozenProject.model_validate_json(project.content).files
                payload["repair_source"] = files.get(request.eda_repair_path, "")[:65_000]
        remaining = CONTEXT_PROFILES[Purpose.REFLECTION].section_budgets["artifact_details"]
        query = " ".join((
            contract.goal, payload.get("user_request", ""), *(criterion.description for criterion in contract.acceptance_criteria),
            *(item.summary for item in observations[-2:]),
        ))
        artifacts = zip(payload["artifacts"], (*inputs, *candidates), strict=True)
        for view, item in sorted(artifacts, key=lambda pair: pair[1].artifact.artifact_type != "rtl_snapshot"):
            if item.artifact.artifact_type == "rtl_snapshot":
                source = self._rtl_excerpt(item, query, min(1_800, remaining) - 8)
                fields = {"rtl_source": source} if source is not None else {}
            else:
                summary = self._artifact_summary(item, min(1_000, remaining))
                fields = {"text": summary} if summary is not None else {}
            if fields:
                view.update(fields)
                remaining -= tokens.count(repo.db.dumps(fields))
        return payload

    def _rtl_excerpt(self, item: ArtifactPayload, query: str, limit: int) -> dict | None:
        try:
            source = TaskVerifierService._snapshot(item)
        except (KeyError, ValueError):
            return None
        content = item.content.decode("utf-8")
        lines = content.splitlines()
        excerpts = [{"start_line": 1, "end_line": len(lines), "text": content}]
        details = {
            "relative_path": source.relative_path, "content_hash": source.content_hash,
            "total_lines": source.line_count, "truncated": False, "excerpts": excerpts,
        }
        tokens = self.tokens
        if tokens.count(self.db.dumps(details)) <= limit:
            return details
        details["truncated"] = True
        excerpts.clear()
        if tokens.count(self.db.dumps(details)) > limit:
            return None
        terms = set(re.findall(r"[a-z_][a-z_0-9]{2,}", query.lower()))
        windows = [(start, "\n".join(lines[start:start + 24])) for start in range(0, len(lines), 24)]
        windows.sort(key=lambda part: -len(terms.intersection(re.findall(r"[a-z_][a-z_0-9]{2,}", part[1].lower()))))
        for start, body in windows:
            excerpts.append({"start_line": start + 1, "end_line": min(len(lines), start + 24), "text": body})
            if tokens.count(self.db.dumps(details)) > limit:
                excerpts.pop()
            if len(excerpts) == 3:
                break
        excerpts.sort(key=lambda part: part["start_line"])
        return details

    @staticmethod
    def _budget(**usage: tuple[int, int]) -> dict[str, int]:
        return {
            f"{name}_{field}": value for name, (used, limit) in usage.items()
            for field, value in (("used", used), ("limit", limit), ("remaining", max(0, limit - used)))
        }

    def _artifact_summary(self, item: ArtifactPayload, limit: int) -> str | None:
        tokens = self.tokens
        if limit <= 40:
            return None
        try:
            TaskVerifierService._check_content(item)
            content = item.content.decode("utf-8")
            if item.artifact.artifact_type == "project_manifest":
                project = FrozenProject.model_validate_json(item.content)
                content = self.db.dumps({
                    "manifest": project.manifest.model_dump(mode="json"), "input_hash": project.input_hash,
                    "file_hashes": project.hashes,
                })
            elif item.artifact.artifact_type == "eda_result":
                receipt = json.loads(content)
                result = receipt["result"]
                fields = ("tool_name", "tool_version", "status", "exit_code", "input_hash", "metrics")
                content = self.db.dumps({
                    "tool_run_id": receipt["tool_run_id"], "result": {key: result[key] for key in fields if key in result},
                })
        except (KeyError, TypeError, ValueError):
            return None
        summary = tokens.trim(content, limit - 24)
        if tokens.count(self.db.dumps({"text": summary})) > limit:
            summary = tokens.trim(content, (limit - 24) // 2)
        return summary if tokens.count(self.db.dumps({"text": summary})) <= limit else None

    def save_manifest(
        self,
        session_id: str,
        run_id: str,
        agent_role: str,
        result: ContextBuildResult,
    ) -> ContextManifestView:
        context_id = str(uuid4())
        created_at = utc_now()
        self.db.execute(
            """INSERT INTO context_manifests
            (context_id, session_id, run_id, agent_role, token_budget, estimated_tokens,
             message_ids_json, memory_ids_json, chunk_ids_json, observation_ids_json,
             turn_summary_ids_json, raw_turn_ids_json, sections_json, summary_json,
             excluded_json, prompt_hash, context_version,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, 3, ?)""",
            (
                context_id,
                session_id,
                run_id,
                agent_role,
                self.token_budget,
                result.estimated_tokens,
                self.db.dumps(result.message_ids),
                self.db.dumps(result.memory_ids),
                self.db.dumps(result.chunk_ids),
                self.db.dumps(result.turn_summary_ids),
                self.db.dumps(result.raw_turn_ids),
                self.db.dumps(result.sections),
                self.db.dumps(result.summary),
                self.db.dumps(result.excluded),
                result.prompt_hash,
                created_at,
            ),
        )
        return self.get_manifest(context_id)  # type: ignore[return-value]

    def get_manifest(self, context_id: str) -> ContextManifestView | None:
        row = self.db.fetch_one(
            "SELECT * FROM context_manifests WHERE context_id = ?", (context_id,)
        )
        return self._manifest(row) if row else None

    def build_invocation(
        self, request: ContextBuildRequest, *, instructions: str = "", tools: list[dict] | None = None,
        expected_shape: str | None = None, output_schema: dict | None = None,
        output_reserve: int = 0, expanded_reflection: bool = False,
    ) -> InvocationContext:
        profile = CONTEXT_PROFILES[request.purpose]
        sources, layout, excluded = self._invocation_sources(request, expanded_reflection)
        selected: dict = {}
        sections: dict = {}
        reserve = max(self.budget.output_reserve, output_reserve)
        budget = self.token_budget if request.purpose == Purpose.MODEL_HEALTH_CHECK else max(0, self.token_budget - reserve)
        limits = {
            "history": self.budget.recent_messages + self.budget.summary,
            "documents": self.budget.documents,
            "long_term_memory": self.budget.long_term_memory,
            "related_memories": self.budget.long_term_memory,
            "project_configuration": self.budget.project_config,
            **profile.section_budgets,
        }

        def assemble() -> dict:
            rendered = dict(selected)
            if "history" in rendered:
                rendered["history"] = [message for group in rendered["history"] for message in group["messages"]]
            if "context_manifest" in rendered:
                refs = self._included_sources(request, selected)
                metadata = rendered["context_manifest"]
                rendered["context_manifest"] = {
                    "context_id": metadata.get("context_id"),
                    **{field: refs.get(field, []) for field in
                       ("message_ids", "raw_turn_ids", "turn_summary_ids", "memory_ids", "chunk_ids")},
                }
                if "project_configuration" in selected:
                    rendered["context_manifest"]["project_config"] = metadata.get("project_config", {})
            body = layout(rendered)
            if output_schema is not None:
                body["output_schema"] = output_schema
            return body

        fixed = {
            "system_prompt": self.tokens.count(instructions),
            "tool_schema": self.tokens.count(self.db.dumps(tools)) if tools else 0,
            "output_contract": self.tokens.count(expected_shape or ""),
            "json_instructions": self.tokens.count(_JSON_PREFIX) if expected_shape is not None else 0,
        }
        framing = 12 + 4 * len(tools or ())

        def estimate(body: dict) -> int:
            return framing + sum(fixed.values()) + self.tokens.count(self.db.dumps(body))

        for name in sorted(sources.keys() - set(profile.allowed_sources)):
            excluded.append({"category": name, "reason": "profile"})
        for name in profile.required_sources:
            if name not in sources:
                raise ValueError(f"{request.purpose}: missing required context section {name}")
            selected[name] = sources[name]
            sections[name] = {"tokens": self.tokens.count(self.db.dumps(sources[name]))}
        required = estimate(assemble())
        if required > budget:
            raise ContextBudgetExceeded(f"{request.purpose}: required input needs {required} tokens, budget is {budget}")

        for name in profile.optional_sources:
            if name not in sources:
                continue
            value = sources[name]
            limit = limits.get(name, budget)
            if isinstance(value, list):
                latest = name in {"recent_turns", "history", "observations", "previous_observations"}
                items = list(reversed(value)) if latest else value
                count = len(items)
                if name in {"observations", "previous_observations"}:
                    count = min(count, profile.max_observations)
                elif name in {"recent_turns", "history"}:
                    count = min(count, profile.max_history_turns)
                selected[name] = []
                for item in items[:count]:
                    old = selected[name]
                    trial = [item, *old] if latest else [*old, item]
                    selected[name] = trial
                    if self.tokens.count(self.db.dumps(trial)) > limit or estimate(assemble()) > budget:
                        selected[name] = old
                        break
                if len(selected[name]) < len(value):
                    excluded.append({"category": name, "reason": "budget_or_count_limit",
                                     "omitted": len(value) - len(selected[name])})
                if estimate(assemble()) > budget:
                    selected.pop(name)
            else:
                selected[name] = value
                if self.tokens.count(self.db.dumps(value)) > limit or estimate(assemble()) > budget:
                    selected.pop(name)
                    excluded.append({"category": name, "reason": "token_budget"})
            if name in selected:
                sections[name] = {"tokens": self.tokens.count(self.db.dumps(selected[name]))}

        if "project_configuration" not in selected:
            selected.pop("project_configuration_guidance", None)
            sections.pop("project_configuration_guidance", None)
        payload = assemble()
        sections.update({name: {"tokens": count} for name, count in fixed.items() if count})
        if output_schema is not None:
            sections["output_schema"] = {"tokens": self.tokens.count(self.db.dumps(output_schema))}
        config = next((selected[name] for name in ("project_configuration", "current_config", "project_config")
                       if isinstance(selected.get(name), dict)), None)
        if config is not None:
            sections["project_config"] = {"revision": config.get("revision")}
        encoded = self.db.dumps({
            "instructions": instructions, "payload": payload, "tools": tools, "expected_shape": expected_shape,
        })
        return InvocationContext(
            context_id=str(uuid4()), purpose=request.purpose, payload=payload,
            estimated_tokens=estimate(payload), token_budget=budget, sections=sections,
            included_sources=self._included_sources(request, selected),
            excluded_sources=excluded, prompt_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            base_context_id=request.base_context_id,
        )

    def _invocation_sources(self, request: ContextBuildRequest, expanded_reflection: bool):
        """Turn scoped business inputs into named sources and a model-facing layout."""
        inputs = request.task_input
        excluded: list[dict] = []
        if request.purpose == Purpose.MODEL_HEALTH_CHECK:
            return {"probe": _PROBE}, dict, excluded
        if request.purpose == Purpose.CONFIG_EXTRACTION and "baseline" in inputs:
            baseline, message = inputs["baseline"], inputs["source"]
            turns = self.sessions.completed_turns(
                message.session_id, agent_role=request.agent_role, before_sequence=message.sequence,
                limit=CONTEXT_PROFILES[request.purpose].max_history_turns,
            )
            config, omitted = config_context(baseline, request.module_name, message.content, self.tokens, 4_000)
            if omitted:
                excluded.append({"category": "current_config", "reason": "section_token_budget", "omitted": omitted})
            sources = {
                "project_id": request.project_id, "module_name": normalize_module(request.module_name),
                "current_request": message.content, "current_config": config or baseline.context(request.module_name),
                "recent_turns": [
                    {"user": turn.user.content, "assistant": turn.assistant.content,
                     "message_ids": [turn.user.message_id, turn.assistant.message_id]} for turn in turns
                ],
            }
            return sources, lambda selected: {
                **selected, "recent_turns": [
                    {"user": turn["user"], "assistant": turn["assistant"]} for turn in selected.get("recent_turns", [])
                ],
            }, excluded
        if request.purpose == Purpose.MEMORY_EXTRACTION and "extraction" in inputs:
            source = inputs["extraction"]
            return {
                "run_id": source.run_id, "project_id": source.project_id, "module_name": source.module_name,
                "user_request": source.user_request, "assistant_response": source.assistant_response,
                "run_summary": source.run_summary, "project_config": source.project_config,
                "related_memories": [
                    {"memory_id": memory.memory_id, "content": memory.content}
                    for memory in source.related_memories.values()
                ],
            }, dict, excluded
        if request.purpose in {Purpose.CHAT_BASE, Purpose.CHAT_RESPONSE, Purpose.INTENT_ENRICHMENT,
                               Purpose.TOOL_SELECTOR, Purpose.TOOL_CALL} and "request" in inputs:
            return self._chat_sources(request), dict, excluded
        if request.purpose == Purpose.GLOBAL_PLANNING:
            base = inputs["base"]
            policy = inputs["policy"]
            slots = ("raw_request", "intent", "catalog")
            sources = {"raw_request": request.query, "intent": inputs["intent"].model_dump(mode="json"),
                       "catalog": inputs["catalog"]}
            sources.update(self._stable_sources(base))
            sources["workflow_contract"] = base.get("workflow_contract", {})
            if "source" in base:
                sources["source"] = base["source"]
            sources["planning_policy"] = {
                "project_id": request.project_id, "module_name": request.module_name, "primary_agent": request.agent_role,
                "input_types": policy.input_types, "rule_ids": policy.rule_ids, "schema_ids": policy.schema_ids,
                "user_constraint_ids": policy.user_constraint_ids, "allowed_mutability": policy.allowed_mutability,
                "max_tasks": policy.run_limits.max_plan_tasks, "validation_feedback": inputs["validation_feedback"],
            }

            def planning_layout(selected: dict) -> dict:
                context = {key: base[key] for key in ("project_id", "module_name", "message", "clarifications") if key in base}
                for model_key, snapshot_key in self._stable_keys().items():
                    if model_key in selected:
                        context[snapshot_key] = selected[model_key]
                context["workflow_contract"] = selected["workflow_contract"]
                if "source" in selected:
                    context["source"] = selected["source"]
                return {**{key: selected[key] for key in slots}, **selected["planning_policy"], "context": context}

            return sources, planning_layout, excluded
        if request.purpose == Purpose.REPLANNING:
            sources = {
                **inputs["execution"], "plan": inputs["plan"].model_dump(mode="json"),
                "trigger": inputs["trigger"].model_dump(mode="json"), "catalog": inputs["catalog"],
                "policy": inputs["policy"].model_dump(mode="json"),
            }
            sources["original_goal"] = {
                key: sources.pop(key) for key in ("raw_request", "module_name", "rtl_path", "clarifications", "validation_feedback")
                if key in sources
            }
            return sources, lambda selected: {**selected["original_goal"], **{
                key: value for key, value in selected.items() if key != "original_goal"
            }}, excluded
        if request.purpose == Purpose.REFLECTION:
            return self._reflection_sources(request, expanded_reflection)
        if request.purpose in {Purpose.SPECIALIST_ANALYSIS, Purpose.VERIFICATION, Purpose.DS_GENERATION,
                               Purpose.RTL_GENERATION, Purpose.INDEPENDENT_REVIEW}:
            return self._professional_sources(request)
        if request.purpose == Purpose.CONVERSATION_SUMMARY:
            turn = inputs["turn"]
            return {"turn": {
                "turn_id": turn.turn_id,
                "user": {"message_id": turn.user.message_id, "content": turn.user.content},
                "assistant": {"message_id": turn.assistant.message_id, "content": turn.assistant.content},
            }}, lambda selected: dict(selected["turn"]), excluded
        if request.purpose == Purpose.SUMMARY_COMPRESSION:
            return {"summary": inputs["summary"]}, lambda selected: {
                **selected, "source_message_ids": inputs["source_message_ids"],
            }, excluded
        sources = dict(inputs)
        if "history" in sources:
            sources["history"] = self._history_groups(sources["history"], sources.get("context_manifest", {}))
        return sources, dict, excluded

    def specialist_scope(self, request: ContextBuildRequest, delegation: DelegationRequest) -> dict:
        source = request.task_input["request"]
        memories = self.memories.retrieve(
            request.project_id, request.module_name, delegation.instruction, agent_role=request.agent_role,
            shared_only=True,
        )
        return {
            "project_id": request.project_id, "module_name": request.module_name, "message": delegation.instruction,
            "instruction": delegation.instruction, "documents": source.get("documents", []), "history": [],
            "_memory_context": [
                {"memory_id": item.memory_id, "memory_key": item.memory_key, "kind": item.kind,
                 "content": item.content, "authority": item.authority, "sources": item.source_refs}
                for item in memories if item.visibility == "project_shared"
            ],
            "workspace_id": source.get("workspace_id"), "delegation": delegation.model_dump(mode="json"),
        }

    def _professional_sources(self, request: ContextBuildRequest):
        inputs = request.task_input
        source = inputs.get("request", {})
        analysis = inputs.get("rtl_analysis")
        if analysis is not None:
            sources = {
                "instruction": analysis["task_goal"],
                "source": {"snapshot": analysis["snapshot"], "source_text": analysis["source_text"]},
                "long_term_memory": analysis.get("shared_memory", []),
            }
            if request.purpose == Purpose.VERIFICATION:
                sources["design_analysis"] = analysis["design_analysis"]

            def rtl_layout(selected: dict) -> dict:
                payload = {"task_goal": selected["instruction"], **selected["source"]}
                if "design_analysis" in selected:
                    payload["design_analysis"] = selected["design_analysis"]
                if "shared_memory" in analysis and "long_term_memory" in selected:
                    payload["shared_memory"] = selected["long_term_memory"]
                return payload

            return sources, rtl_layout, []

        sources = self._stable_sources(source)
        sources.update({key: value for key, value in inputs.items() if key != "request"})
        sources["instruction"] = source.get("instruction") or source.get("message") or request.query
        scope = {key: source[key] for key in ("project_id", "module_name") if key in source}
        expert = request.purpose in {Purpose.SPECIALIST_ANALYSIS, Purpose.VERIFICATION}
        if expert:
            sources["source"] = {}
            if request.purpose == Purpose.VERIFICATION:
                sources["design_analysis"] = None
            sources.pop("project_configuration", None)
            sources.pop("context_manifest", None)
            if not source.get("delegation"):
                sources.pop("long_term_memory", None)
            elif source["delegation"].get("artifacts"):
                sources["artifacts"] = source["delegation"]["artifacts"]
        elif request.purpose == Purpose.INDEPENDENT_REVIEW:
            sources.pop("long_term_memory", None)
            sources.pop("context_manifest", None)
        if sources.get("project_configuration"):
            sources["project_configuration_guidance"] = _CONFIG_GUIDANCE

        def layout(selected: dict) -> dict:
            payload = {**scope, **selected}
            if expert:
                payload["history"] = []
                payload.pop("source", None)
                if payload.get("design_analysis") is None:
                    payload.pop("design_analysis", None)
            return payload

        return sources, layout, []

    @staticmethod
    def _stable_keys() -> dict[str, str]:
        return {
            "history": "history", "documents": "documents", "project_configuration": "_project_config",
            "long_term_memory": "_memory_context", "context_manifest": "_context_manifest",
        }

    def _stable_sources(self, snapshot: dict) -> dict:
        sources = {key: snapshot[value] for key, value in self._stable_keys().items() if value in snapshot}
        if "history" in sources:
            sources["history"] = self._history_groups(sources["history"], sources.get("context_manifest", {}))
        return sources

    @staticmethod
    def _history_groups(history: list[dict | str], manifest: dict) -> list[dict]:
        messages = iter(manifest.get("message_ids", []))
        raw = iter(manifest.get("raw_turn_ids", []))
        summaries = iter(manifest.get("turn_summary_ids", []))
        groups = []
        index = 0
        while index < len(history):
            entry = history[index]
            following = history[index + 1] if index + 1 < len(history) else None
            role = entry.get("role") if isinstance(entry, dict) else None
            if role == "user" and isinstance(following, dict) and following.get("role") == "assistant":
                groups.append({
                    "messages": history[index:index + 2], "raw_turn_ids": [next(raw, None)],
                    "message_ids": [next(messages, None), next(messages, None)],
                })
                index += 2
                continue
            item = {"messages": [entry]}
            if role == "assistant" and entry["content"].startswith("[独立轮次摘要 turn_id="):
                item["turn_summary_ids"] = [next(summaries, None)]
            groups.append(item)
            index += 1
        return groups

    def _chat_sources(self, request: ContextBuildRequest) -> dict:
        inputs = request.task_input
        source = inputs["request"]
        purpose = request.purpose
        message = source.get("message", request.query)
        observations = inputs.get("observations", [])
        if purpose == Purpose.TOOL_SELECTOR:
            names = set(request.authorized_tool_names)
            metadata_fields = ("name", "description", "when_to_use", "when_not_to_use", "category", "always_available")
            return {
                "primary_agent": request.agent_role,
                "task_intent": inputs["intent"],
                "task_summary": message,
                "has_observations": bool(observations),
                "authorized_tool_metadata": [
                    {key: tool[key] for key in metadata_fields if key in tool}
                    for tool in inputs["tool_metadata"] if tool["name"] in names
                ],
                "max_tools": inputs["max_tools"],
            }
        result = self._stable_sources(source)
        result.update(
            project_id=source.get("project_id"), module_name=source.get("module_name"),
            instruction=source.get("instruction") or message,
        )
        if source.get("_project_config"):
            result["project_configuration_guidance"] = _CONFIG_GUIDANCE
        if purpose in {Purpose.CHAT_BASE, Purpose.CHAT_RESPONSE}:
            result["current_message"] = message
        else:
            result.update(raw_request=message, workspace_id=source.get("workspace_id"),
                          has_confirmed_ds=bool(source.get("ds_markdown")))
            if purpose == Purpose.INTENT_ENRICHMENT:
                result["available_tools"] = list(request.authorized_tool_names)
            else:
                result.update(
                    task_intent=inputs["intent"], previous_observations=observations,
                    already_called_tools=[item.get("tool_name") for item in observations],
                    primary_agent=request.agent_role,
                )
        return result

    def _reflection_sources(self, request: ContextBuildRequest, expanded: bool):
        contract = request.task_input["contract"]
        dynamic = dict(request.task_input["dynamic"])
        dynamic.setdefault("verification_feedback", None)
        conversation = dynamic.pop("conversation", None)
        excluded = []
        artifacts = dynamic.pop("artifacts", [])
        if not expanded and any("rtl_source" in item for item in artifacts):
            excluded.append({"category": "rtl_source", "reason": "endpoint_scope"})
        metadata, details = [], []
        for item in artifacts:
            snippets = {key: item[key] for key in ("text", "rtl_source") if key in item and (key != "rtl_source" or expanded)}
            metadata.append({key: value for key, value in item.items() if key not in {"text", "rtl_source"}})
            if snippets:
                details.append({"artifact_id": item["artifact_id"], **snippets})
        details.sort(key=lambda item: "rtl_source" not in item)
        dynamic["artifacts"] = metadata
        dynamic["artifact_details"] = details
        if not expanded:
            feedback = dynamic.pop("previous_attempt_feedback", None)
            if not dynamic.get("replanning_enabled"):
                dynamic.pop("plan_tasks", None)
            elif feedback and (feedback.get("failed_criteria") or feedback.get("suggestions")):
                dynamic["previous_attempt_feedback"] = {
                    "failed_criteria": feedback.get("failed_criteria", []), "suggestions": feedback.get("suggestions", []),
                }
            if feedback:
                excluded.append({"category": "previous_attempt_failure_reason", "reason": "endpoint_scope"})
        sources = self._stable_sources(conversation or {})
        sources.update({key: dynamic.pop(key) for key in CONTEXT_PROFILES[Purpose.REFLECTION].allowed_sources if key in dynamic})
        state_fields = {
            "message", "rtl_path", "replanning_enabled", "output_guidance", "user_request", "clarifications",
            "verilator_lint_arguments", "eda_arguments", "repair_source",
        }
        excluded.extend({"category": key, "reason": "profile"} for key in sorted(dynamic.keys() - state_fields))
        sources.update(
            contract=contract.model_dump(mode="json"),
            task_state={key: value for key, value in dynamic.items() if key in state_fields},
            visible_evidence=[item.model_dump(mode="json") for item in request.evidence_refs],
        )

        def reflection_layout(selected: dict) -> dict:
            context = dict(selected["task_state"])
            stable = self._stable_keys()
            artifact_details = {item["artifact_id"]: item for item in selected.get("artifact_details", [])}
            for key, value in selected.items():
                if key in {"contract", "task_state", "visible_evidence", "artifacts", "artifact_details"} or (
                    conversation is not None and key in stable
                ):
                    continue
                context[key] = value
            context["artifacts"] = [{**item, **artifact_details.get(item["artifact_id"], {})} for item in selected["artifacts"]]
            if conversation is not None:
                context["conversation"] = {
                    **{key: conversation[key] for key in ("project_id", "module_name", "message") if key in conversation},
                    **{value: selected[key] for key, value in stable.items() if key in selected},
                }
            return {"contract": selected["contract"], "context": context, "visible_evidence": selected["visible_evidence"]}

        return sources, reflection_layout, excluded

    @staticmethod
    def _included_sources(request: ContextBuildRequest, selected: dict) -> dict[str, list[str]]:
        refs: dict[str, list[str]] = {}

        def add(field: str, value: str | None) -> None:
            if value:
                values = refs.setdefault(field, [])
                if value not in values:
                    values.append(value)

        add("message_ids", request.source_message_id)
        if request.purpose == Purpose.MEMORY_EXTRACTION and "extraction" in request.task_input:
            for message_id in request.task_input["extraction"].message_ids:
                add("message_ids", message_id)
        if request.purpose == Purpose.SUMMARY_COMPRESSION:
            for message_id in request.task_input["source_message_ids"]:
                add("message_ids", message_id)
        if isinstance(selected.get("source"), dict):
            add("artifact_ids", selected["source"].get("snapshot", {}).get("content_ref"))
        keys = {
            "message_id": "message_ids", "memory_id": "memory_ids", "chunk_id": "chunk_ids",
            "observation_id": "observation_ids", "artifact_id": "artifact_ids",
        }
        list_fields = {*keys.values(), "raw_turn_ids", "turn_summary_ids"}

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in keys and isinstance(child, str):
                        add(keys[key], child)
                    elif key in list_fields and isinstance(child, list):
                        for item in child:
                            if isinstance(item, str):
                                add(key, item)
                    else:
                        collect(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect(child)

        for name in ("history", "long_term_memory", "related_memories", "observations", "previous_observations",
                     "artifacts", "accepted_artifacts", "turn", "run_summary", "tasks", "recent_turns",
                     "verification_feedback"):
            collect(selected.get(name))
        evidence = selected.get("visible_evidence", ()) or selected.get("trigger", {}).get("evidence", ())
        for item in evidence:
            kind = {"observation": "observation_ids", "artifact": "artifact_ids",
                    "tool_run": "tool_run_ids", "verification": "verification_ids"}.get(item["kind"])
            if kind:
                add(kind, item["ref_id"])
        for document in selected.get("documents", []):
            content = document.get("content", "")
            locator = re.match(r"\[RAG source: chunk=([a-f0-9-]{36});", content)
            if locator:
                add("chunk_ids", locator[1])
        return refs

    def save_invocation_manifest(self, connection, request: ContextBuildRequest,
                                 context: InvocationContext, invocation_id: str) -> None:
        columns = ("message_ids", "memory_ids", "chunk_ids", "observation_ids", "artifact_ids",
                   "turn_summary_ids", "raw_turn_ids")
        connection.execute(
            """INSERT INTO context_manifests
            (context_id, session_id, run_id, agent_role, token_budget, estimated_tokens,
             message_ids_json, memory_ids_json, chunk_ids_json, observation_ids_json,
             artifact_ids_json, turn_summary_ids_json, raw_turn_ids_json, sections_json,
             summary_json, excluded_json, prompt_hash, context_version, created_at,
             orchestration_task_id, purpose, base_context_id, profile_version, invocation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 5, ?, ?, ?, ?, ?, ?)""",
            (context.context_id, request.session_id, request.run_id, request.agent_role or "system",
             context.token_budget, context.estimated_tokens,
             *(self.db.dumps(context.included_sources.get(name, [])) for name in columns),
             self.db.dumps(context.sections), self.db.dumps({
                 "generation": request.generation,
                 **{key: values for key, values in context.included_sources.items() if key not in columns},
             }),
             self.db.dumps(context.excluded_sources), context.prompt_hash, utc_now(), request.task_id,
             context.purpose, context.base_context_id, CONTEXT_PROFILES[context.purpose].version, invocation_id),
        )

    def list_for_run(self, run_id: str) -> list[ContextManifestView]:
        rows = self.db.fetch_all(
            "SELECT * FROM context_manifests WHERE run_id = ? ORDER BY created_at", (run_id,)
        )
        return [self._manifest(row) for row in rows]

    def _manifest(self, row: dict) -> ContextManifestView:
        payload = dict(row)
        for field in (
            "message_ids", "memory_ids", "chunk_ids", "observation_ids", "artifact_ids",
            "turn_summary_ids", "raw_turn_ids", "sections", "summary", "excluded",
        ):
            default = {} if field in {"sections", "summary"} else []
            payload[field] = self.db.loads(payload.pop(f"{field}_json"), default)
        return ContextManifestView(**payload)


context_builder_service = ContextBuilderService()
