"""完整问答轮次的独立摘要与有界后台任务队列。"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.config.model_settings import model_settings_service
from app.domain.models import ConversationTurnSummaryView
from app.models.factory import provider_factory
from app.services.session_service import ConversationTurn, SessionService, session_service
from app.services.token_service import TokenCounter, token_counter
from app.services.workspace_service import utc_now


SUMMARY_LIST_FIELDS = (
    "key_information", "decisions", "constraints", "assumptions",
    "inferences", "open_questions", "keywords",
)


@dataclass(frozen=True)
class SummaryPolicy:
    recent_turns: int = 5
    history_turns: int = 30
    target_min_tokens: int = 150
    target_max_tokens: int = 250
    hard_max_tokens: int = 500
    summary_budget: int = 6_000
    max_workers: int = 2
    max_pending_tasks: int = 32


class ConversationSummaryService:
    """每轮只读取该轮两条消息；失败只更新任务状态，不影响正常对话。"""

    def __init__(
        self,
        sessions: SessionService = session_service,
        tokens: TokenCounter = token_counter,
        policy: SummaryPolicy = SummaryPolicy(),
    ) -> None:
        self.sessions = sessions
        self.db = sessions.db
        self.tokens = tokens
        self.policy = policy
        self._executor = ThreadPoolExecutor(
            max_workers=policy.max_workers, thread_name_prefix="chipflow-summary"
        )
        self._slots = threading.BoundedSemaphore(policy.max_pending_tasks)
        self._lock = threading.Lock()
        self._scheduled: set[str] = set()

    def enqueue_turn(self, turn_id: str, session_id: str, agent_role: str) -> bool:
        """非阻塞投递；队列满时保留 pending，之后可显式重试。"""
        turn = self.sessions.get_turn(turn_id, agent_role=agent_role)
        if turn is None or turn.session_id != session_id:
            return False
        self._ensure_record(turn)
        existing = self.get_turn_summary(turn_id)
        if existing and existing.status == "completed":
            return True
        with self._lock:
            if turn_id in self._scheduled:
                return True
            if not self._slots.acquire(blocking=False):
                return False
            self._scheduled.add(turn_id)
        try:
            future = self._executor.submit(self.summarize_turn, turn_id, agent_role)
        except Exception:
            self._release_slot(turn_id)
            raise
        future.add_done_callback(lambda _: self._release_slot(turn_id))
        return True

    def summarize_turn(
        self, turn_id: str, agent_role: str | None = None
    ) -> ConversationTurnSummaryView:
        """幂等生成指定完整轮次摘要；既支持同步测试，也支持后台调用。"""
        existing = self.get_turn_summary(turn_id)
        role = agent_role or (existing.agent_role if existing else None)
        if not role:
            raise KeyError(turn_id)
        turn = self.sessions.get_turn(turn_id, agent_role=role)
        if turn is None:
            raise KeyError(turn_id)
        self._ensure_record(turn)
        existing = self.get_turn_summary(turn_id)
        if existing and existing.status == "completed":
            return existing

        self.db.execute(
            """UPDATE conversation_turn_summaries
            SET status = 'processing', attempts = attempts + 1, last_error = NULL,
                updated_at = ? WHERE turn_id = ? AND status <> 'completed'""",
            (utc_now(), turn_id),
        )
        try:
            summary, mode = self._generate(turn)
            rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            token_count = self.tokens.count(rendered)
            if token_count > self.policy.hard_max_tokens:
                summary = self._compress(turn, summary)
                rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
                token_count = self.tokens.count(rendered)
            if token_count > self.policy.hard_max_tokens:
                raise ValueError(
                    f"turn summary exceeds hard limit: {token_count} > {self.policy.hard_max_tokens}"
                )
            self.db.execute(
                """UPDATE conversation_turn_summaries
                SET summary_json = ?, token_count = ?, status = 'completed',
                    generation_mode = ?, last_error = NULL, updated_at = ?
                WHERE turn_id = ?""",
                (self.db.dumps(summary), token_count, mode, utc_now(), turn_id),
            )
        except Exception as exc:
            self.db.execute(
                """UPDATE conversation_turn_summaries
                SET status = 'failed', last_error = ?, updated_at = ? WHERE turn_id = ?""",
                (f"{exc.__class__.__name__}: {str(exc)[:500]}", utc_now(), turn_id),
            )
        result = self.get_turn_summary(turn_id)
        if result is None:
            raise RuntimeError("turn summary record disappeared")
        return result

    def get_recent_turn_summaries(
        self,
        session_id: str,
        agent_role: str,
        *,
        before_sequence: int | None = None,
        limit: int = 30,
    ) -> list[ConversationTurnSummaryView]:
        """只读 completed；数据库保留所有历史，这里仅限制本次窗口。"""
        parameters: list[object] = [session_id, agent_role]
        before = ""
        if before_sequence is not None:
            before = " AND sequence < ?"
            parameters.append(before_sequence)
        parameters.append(max(1, min(limit, 100)))
        rows = self.db.fetch_all(
            f"""SELECT * FROM conversation_turn_summaries
            WHERE session_id = ? AND agent_role = ? AND status = 'completed'{before}
            ORDER BY sequence DESC LIMIT ?""",
            tuple(parameters),
        )
        return [self._view(row) for row in reversed(rows)]

    def get_turn_summary(self, turn_id: str) -> ConversationTurnSummaryView | None:
        row = self.db.fetch_one(
            "SELECT * FROM conversation_turn_summaries WHERE turn_id = ?", (turn_id,)
        )
        return self._view(row) if row else None

    def retry_failed_summaries(
        self, *, session_id: str | None = None, limit: int = 20
    ) -> int:
        """重新投递 failed/pending；唯一键与进程内集合共同防止重复任务。"""
        parameters: list[object] = []
        session_filter = ""
        if session_id:
            session_filter = " AND session_id = ?"
            parameters.append(session_id)
        parameters.append(max(1, min(limit, 100)))
        rows = self.db.fetch_all(
            f"""SELECT turn_id, session_id, agent_role FROM conversation_turn_summaries
            WHERE status IN ('failed', 'pending'){session_filter}
            ORDER BY sequence LIMIT ?""",
            tuple(parameters),
        )
        return sum(
            1 for row in rows
            if self.enqueue_turn(row["turn_id"], row["session_id"], row["agent_role"])
        )

    def _ensure_record(self, turn: ConversationTurn) -> None:
        now = utc_now()
        self.db.execute(
            """INSERT INTO conversation_turn_summaries
            (id, turn_id, session_id, agent_role, sequence, user_message_id,
             assistant_message_id, summary_json, token_count, status, attempts,
             generation_mode, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 0, 'pending', 0,
                    'model_structured', NULL, ?, ?)
            ON CONFLICT(turn_id) DO NOTHING""",
            (
                str(uuid4()), turn.turn_id, turn.session_id, turn.agent_role,
                turn.sequence, turn.user.message_id, turn.assistant.message_id, now, now,
            ),
        )

    def _generate(self, turn: ConversationTurn) -> tuple[dict[str, Any], str]:
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        settings = model_settings_service.effective()
        if mode in {"test", "mock", "deterministic"}:
            return self._deterministic_summary(turn), "deterministic_turn"
        if not settings.ready:
            raise RuntimeError("turn summary model is unavailable")
        result = provider_factory.get(settings.main_endpoint()).generate_json(
            """你是芯片设计项目的单轮对话摘要器。只总结输入中的这一条 User Request 和对应的最终 Assistant Response，不读取也不合并任何旧摘要。输出严格 JSON，字段为 topic、module、user_intent、key_information、decisions、constraints、assumptions、inferences、open_questions、keywords。目标 150-250 Token，绝不超过 500 Token。严格区分用户明确确认的信息与 Assistant 建议：decisions 和 constraints 中每项使用 {\"content\":\"...\",\"source_message_ids\":[\"...\"],\"confirmation_status\":\"user_confirmed|assistant_suggestion|unconfirmed|historical\"}；只有用户消息明确确认才能标 user_confirmed。被本轮新决定替代的旧信息标 historical，不得当作当前约束。不得编造；无内容字段使用空字符串或空数组。""",
            self._turn_payload(turn),
            operation="单轮会话摘要",
            expected_shape='{"topic":"","module":"","user_intent":"","key_information":[],"decisions":[],"constraints":[],"assumptions":[],"inferences":[],"open_questions":[],"keywords":[]}',
        )
        if not isinstance(result, dict):
            raise ValueError("turn summary output must be an object")
        return self._validate(result, turn), "model_structured"

    def _compress(self, turn: ConversationTurn, summary: dict[str, Any]) -> dict[str, Any]:
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        settings = model_settings_service.effective()
        if mode in {"test", "mock", "deterministic"}:
            raise ValueError("deterministic turn summary unexpectedly exceeded hard limit")
        if not settings.ready:
            raise RuntimeError("turn summary compression model is unavailable")
        result = provider_factory.get(settings.main_endpoint()).generate_json(
            """压缩给定单轮结构化摘要，保持原字段和事实极性，不新增信息，不改变 confirmation_status。删除冗余表述，使完整 JSON 不超过 500 Token，优先达到 150-250 Token。""",
            {"summary": summary, "source_message_ids": [turn.user.message_id, turn.assistant.message_id]},
            operation="压缩单轮会话摘要",
            expected_shape='{"topic":"","module":"","user_intent":"","key_information":[],"decisions":[],"constraints":[],"assumptions":[],"inferences":[],"open_questions":[],"keywords":[]}',
        )
        if not isinstance(result, dict):
            raise ValueError("compressed turn summary must be an object")
        return self._validate(result, turn)

    def _validate(self, result: dict[str, Any], turn: ConversationTurn) -> dict[str, Any]:
        allowed_ids = {turn.user.message_id, turn.assistant.message_id}
        summary: dict[str, Any] = {
            "topic": str(result.get("topic", "")).strip(),
            "module": str(result.get("module", "")).strip(),
            "user_intent": str(result.get("user_intent", "")).strip(),
        }
        for field in SUMMARY_LIST_FIELDS:
            values = result.get(field, [])
            if not isinstance(values, list):
                values = []
            normalized = []
            for value in values:
                if field not in {"decisions", "constraints"}:
                    text = str(value).strip()
                    if text:
                        normalized.append(text)
                    continue
                if not isinstance(value, dict) or not str(value.get("content", "")).strip():
                    continue
                source_ids = [
                    source_id for source_id in value.get("source_message_ids", [])
                    if source_id in allowed_ids
                ]
                status = str(value.get("confirmation_status", "unconfirmed"))
                if status == "user_confirmed" and (
                    turn.user.message_id not in source_ids
                    or turn.assistant.message_id in source_ids
                ):
                    status = "unconfirmed"
                if status == "assistant_suggestion" and turn.assistant.message_id not in source_ids:
                    status = "unconfirmed"
                if status not in {
                    "user_confirmed", "assistant_suggestion", "unconfirmed", "historical"
                }:
                    status = "unconfirmed"
                normalized.append({
                    "content": str(value["content"]).strip(),
                    "source_message_ids": list(dict.fromkeys(source_ids)),
                    "confirmation_status": status,
                })
            summary[field] = normalized
        return summary

    def _deterministic_summary(self, turn: ConversationTurn) -> dict[str, Any]:
        """测试回退不把 Assistant 文本提升成用户确认事实。"""
        intent = " ".join(turn.user.content.split())[:600]
        response = " ".join(turn.assistant.content.split())[:600]
        return {
            "topic": intent[:120], "module": "", "user_intent": intent,
            "key_information": [response] if response else [],
            "decisions": [], "constraints": [], "assumptions": [],
            "inferences": [], "open_questions": [], "keywords": [],
        }

    @staticmethod
    def _turn_payload(turn: ConversationTurn) -> dict[str, Any]:
        return {
            "turn_id": turn.turn_id,
            "user": {"message_id": turn.user.message_id, "content": turn.user.content},
            "assistant": {
                "message_id": turn.assistant.message_id, "content": turn.assistant.content,
            },
        }

    def _view(self, row: dict[str, Any]) -> ConversationTurnSummaryView:
        payload = dict(row)
        payload["summary_json"] = self.db.loads(payload.get("summary_json"), {})
        return ConversationTurnSummaryView(**payload)

    def _release_slot(self, turn_id: str) -> None:
        with self._lock:
            self._scheduled.discard(turn_id)
            self._slots.release()


conversation_summary_service = ConversationSummaryService()
