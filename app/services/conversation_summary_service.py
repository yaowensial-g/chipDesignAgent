"""按角色隔离的增量式会话摘要。"""

from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass
from typing import Any

from app.config.model_settings import model_settings_service
from app.models.factory import provider_factory
from app.services.session_service import SessionService, session_service
from app.services.token_service import TokenCounter, token_counter


SUMMARY_FIELDS = (
    "current_goal",
    "current_stage",
    "confirmed_requirements",
    "important_decisions",
    "completed_work",
    "pending_tasks",
    "open_questions",
    "source_message_ids",
)


@dataclass(frozen=True)
class SummaryPolicy:
    recent_window: int = 24
    batch_messages: int = 10
    trigger_tokens: int = 4_000


class ConversationSummaryService:
    """摘要只压缩即将离开原文窗口的消息，失败时绝不推进覆盖游标。"""

    def __init__(
        self,
        sessions: SessionService = session_service,
        tokens: TokenCounter = token_counter,
        policy: SummaryPolicy = SummaryPolicy(),
    ) -> None:
        self.sessions = sessions
        self.tokens = tokens
        self.policy = policy

    def refresh(self, session_id: str, agent_role: str, recent_messages: list) -> None:
        if not recent_messages:
            return
        # recent_messages 已由 SessionService 使用角色规则过滤且按时间正序返回。
        cutoff = recent_messages[0].sequence - 1
        retained_tokens = 0
        for message in reversed(recent_messages):
            cost = self.tokens.count(message.content)
            if retained_tokens + cost > self.policy.trigger_tokens:
                cutoff = max(cutoff, message.sequence)
            else:
                retained_tokens += cost
        existing = self.sessions.get_summary(session_id, agent_role)
        covered = existing.covered_until_sequence if existing else 0
        if cutoff <= covered:
            return

        while covered < cutoff:
            pending = self.sessions.visible_messages_between(
                session_id,
                agent_role=agent_role,
                after_sequence=covered,
                through_sequence=cutoff,
            )
            if not pending:
                # 中间只有其他角色私有消息；安全跨过不可见序号，不把正文写入摘要。
                covered = cutoff
                self._persist(
                    session_id,
                    agent_role,
                    existing,
                    existing.summary_json if existing else self._empty_summary(),
                    covered,
                    [],
                    "visibility_gap",
                )
                return
            batch = pending[: self.policy.batch_messages]
            try:
                summary, mode = self._summarize(existing, batch)
            except Exception as exc:
                # 保留原覆盖范围，仅记录错误；下一次构建会从同一批消息重试。
                base = existing.summary_json if existing else self._empty_summary()
                self.sessions.upsert_summary(
                    session_id,
                    agent_role,
                    base,
                    existing.summary_text if existing else self._render(base),
                    existing.covered_from_sequence if existing else 0,
                    covered,
                    existing.source_message_ids if existing else [],
                    generation_mode=existing.generation_mode if existing else "failed",
                    last_error=f"{exc.__class__.__name__}: {str(exc)[:500]}",
                )
                return
            covered = batch[-1].sequence
            self._persist(
                session_id,
                agent_role,
                existing,
                summary,
                covered,
                [message.message_id for message in batch],
                mode,
            )
            existing = self.sessions.get_summary(session_id, agent_role)

    def _summarize(self, existing, messages: list) -> tuple[dict[str, Any], str]:
        mode = os.getenv("CHIP_AGENT_RUNTIME_MODE", "production").strip().lower()
        settings = model_settings_service.effective()
        if mode in {"test", "mock", "deterministic"} or not settings.ready:
            return self._deterministic_extract(existing, messages), "deterministic_extract"

        payload = {
            "existing_summary": existing.summary_json if existing else self._empty_summary(),
            "new_messages": [
                {
                    "message_id": item.message_id,
                    "sequence": item.sequence,
                    "role": item.role,
                    "content": item.content,
                }
                for item in messages
            ],
        }
        result = provider_factory.get(settings.main_endpoint()).generate_json(
            """你是会话摘要器。将已有摘要与新增消息合并为结构化 JSON。只记录消息明确表达的目标、阶段、要求、决策、完成项、待办和问题。confirmed_requirements 与 important_decisions 的每一项必须包含 content、source_message_ids、confirmation_status；只有用户明确确认时才可标 user_confirmed，Assistant 建议一律标 unconfirmed。不得虚构批准，不得丢弃仍待处理的任务或开放问题。返回字段：current_goal、current_stage、confirmed_requirements、important_decisions、completed_work、pending_tasks、open_questions、source_message_ids。""",
            payload,
            operation="增量会话摘要",
            expected_shape='{"current_goal":"","current_stage":"","confirmed_requirements":[],"important_decisions":[],"completed_work":[],"pending_tasks":[],"open_questions":[],"source_message_ids":[]}',
        )
        if not isinstance(result, dict):
            raise ValueError("summary output must be an object")
        return self._validate_model_summary(result, existing, messages), "model_structured"

    def _validate_model_summary(self, result: dict, existing, messages: list) -> dict[str, Any]:
        allowed_ids = set(existing.source_message_ids if existing else []) | {
            item.message_id for item in messages
        }
        user_ids = {item.message_id for item in messages if item.role == "user"}
        if existing:
            for field in ("confirmed_requirements", "important_decisions"):
                for item in existing.summary_json.get(field, []):
                    if isinstance(item, dict) and item.get("confirmation_status") == "user_confirmed":
                        user_ids.update(item.get("source_message_ids", []))
        summary = self._empty_summary()
        summary["current_goal"] = str(result.get("current_goal", ""))[:2000]
        summary["current_stage"] = str(result.get("current_stage", ""))[:500]
        for field in ("completed_work", "pending_tasks", "open_questions"):
            summary[field] = [str(item)[:2000] for item in result.get(field, []) if str(item).strip()]
        for field in ("confirmed_requirements", "important_decisions"):
            values = []
            for item in result.get(field, []):
                if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                    continue
                source_ids = [sid for sid in item.get("source_message_ids", []) if sid in allowed_ids]
                status = str(item.get("confirmation_status", "unconfirmed"))
                if status == "user_confirmed" and not user_ids.intersection(source_ids):
                    status = "unconfirmed"
                values.append({
                    "content": str(item["content"])[:2000],
                    "source_message_ids": source_ids,
                    "confirmation_status": status if status in {"user_confirmed", "formal_source", "unconfirmed"} else "unconfirmed",
                })
            summary[field] = values
        summary["source_message_ids"] = list(dict.fromkeys(
            (existing.source_message_ids if existing else [])
            + [item.message_id for item in messages]
        ))
        digests = list(existing.summary_json.get("source_message_digests", [])) if existing else []
        known_digests = {item.get("message_id") for item in digests if isinstance(item, dict)}
        digests.extend({
            "message_id": item.message_id,
            "sequence": item.sequence,
            "role": item.role,
            "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        } for item in messages if item.message_id not in known_digests)
        summary["source_message_digests"] = digests
        return summary

    def _deterministic_extract(self, existing, messages: list) -> dict[str, Any]:
        """离线回退保存可追溯原文，不声称已经完成语义概括。"""
        summary = dict(existing.summary_json) if existing else self._empty_summary()
        unresolved = list(summary.get("unprocessed_semantic_messages", []))
        known = {str(item.get("message_id")) for item in unresolved if isinstance(item, dict)}
        for message in messages:
            if message.message_id not in known:
                unresolved.append({
                    "message_id": message.message_id,
                    "sequence": message.sequence,
                    "role": message.role,
                    "content": message.content,
                })
        summary["unprocessed_semantic_messages"] = unresolved
        summary["source_message_ids"] = list(dict.fromkeys(
            list(summary.get("source_message_ids", []))
            + [item.message_id for item in messages]
        ))
        digests = list(summary.get("source_message_digests", []))
        known_digests = {item.get("message_id") for item in digests if isinstance(item, dict)}
        digests.extend({
            "message_id": item.message_id,
            "sequence": item.sequence,
            "role": item.role,
            "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        } for item in messages if item.message_id not in known_digests)
        summary["source_message_digests"] = digests
        summary["summary_notice"] = "确定性回退仅保存待语义处理的原文，没有宣称完成语义总结。"
        return summary

    def _persist(
        self,
        session_id: str,
        agent_role: str,
        existing,
        summary: dict[str, Any],
        covered_until: int,
        new_ids: list[str],
        mode: str,
    ) -> None:
        all_ids = list(dict.fromkeys(
            (existing.source_message_ids if existing else []) + new_ids
        ))
        summary["source_message_ids"] = all_ids
        self.sessions.upsert_summary(
            session_id,
            agent_role,
            summary,
            self._render(summary),
            existing.covered_from_sequence if existing else (covered_until if all_ids else 0),
            covered_until,
            all_ids,
            generation_mode=mode,
        )

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "current_goal": "",
            "current_stage": "",
            "confirmed_requirements": [],
            "important_decisions": [],
            "completed_work": [],
            "pending_tasks": [],
            "open_questions": [],
            "source_message_ids": [],
        }

    @staticmethod
    def _render(summary: dict[str, Any]) -> str:
        lines = []
        labels = {
            "current_goal": "当前目标",
            "current_stage": "当前阶段",
            "confirmed_requirements": "已确认要求",
            "important_decisions": "重要决策",
            "completed_work": "已完成",
            "pending_tasks": "待办",
            "open_questions": "开放问题",
        }
        for field, label in labels.items():
            value = summary.get(field)
            if not value:
                continue
            if isinstance(value, list):
                rendered = [item.get("content", "") if isinstance(item, dict) else str(item) for item in value]
                lines.append(f"{label}：" + "；".join(item for item in rendered if item))
            else:
                lines.append(f"{label}：{value}")
        unresolved = summary.get("unprocessed_semantic_messages", [])
        if unresolved:
            lines.append("待语义处理原文：")
            lines.extend(
                f"#{item['sequence']} {item['role']}: {item['content']}"
                for item in unresolved if isinstance(item, dict)
            )
        return "\n".join(lines)


conversation_summary_service = ConversationSummaryService()
