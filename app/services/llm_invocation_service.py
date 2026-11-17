"""Build, route, and audit each business model invocation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.config.model_settings import ModelEndpointSettings, model_settings_service
from app.domain.context import ContextBuildRequest, ContextPurpose as Purpose
from app.models.factory import provider_factory
from app.models.provider import InvalidJSONOutput, ModelInvocationError
from app.services.token_usage_service import TokenUsage, token_usage_tracker
from app.services.workspace_service import utc_now

if TYPE_CHECKING:
    from app.services.context_builder_service import ContextBuilderService


DEEPSEEK_DESIGN_DATA_URL = "https://api.deepseek.com"
_REPAIR_PROMPT = "你是 JSON 格式修复器。只修复格式，不新增、删除或改变原始结论。只返回合法 JSON。"
_HEALTH_PROMPT = "你是连接检查器。严格按要求回答。"


class ModelRoute(StrEnum):
    MAIN = "MAIN"
    SELECTOR = "SELECTOR"
    DEEPSEEK_DIRECT = "DEEPSEEK_DIRECT"
    REFLECTION = "REFLECTION"


_PURPOSE_ROUTES = {
    Purpose.CONFIG_EXTRACTION: ModelRoute.DEEPSEEK_DIRECT,
    Purpose.MEMORY_EXTRACTION: ModelRoute.DEEPSEEK_DIRECT,
    Purpose.TOOL_SELECTOR: ModelRoute.SELECTOR,
    Purpose.REFLECTION: ModelRoute.REFLECTION,
}


@dataclass
class InvocationScope:
    contexts: ContextBuilderService
    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    generation: int | None = None
    agent_role: str | None = None
    source_message_id: str | None = None
    base_context_id: str | None = None
    pending: list[str] = field(default_factory=list)

    def attach_run(self, run_id: str, base_context_id: str | None = None) -> None:
        if self.pending:
            with self.contexts.db.transaction() as conn:
                for invocation_id in self.pending:
                    conn.execute("UPDATE llm_invocations SET run_id=? WHERE invocation_id=?", (run_id, invocation_id))
                    conn.execute(
                        """UPDATE context_manifests SET run_id=?, base_context_id=COALESCE(base_context_id, ?)
                           WHERE invocation_id=? AND purpose='INTENT_ENRICHMENT'""",
                        (run_id, base_context_id, invocation_id),
                    )
                    conn.execute(
                        "UPDATE context_manifests SET run_id=? WHERE invocation_id=? AND purpose<>'INTENT_ENRICHMENT'",
                        (run_id, invocation_id),
                    )
            self.pending.clear()
        self.run_id = run_id
        self.base_context_id = base_context_id


_scope: ContextVar[InvocationScope | None] = ContextVar("chipflow_invocation_scope", default=None)


@contextmanager
def invocation_scope(contexts: ContextBuilderService | None = None, *, default_task_id: str | None = None,
                     **scope_fields: Any):
    active = _scope.get()
    if active is not None and (contexts is None or (
        contexts.db is active.contexts.db and scope_fields.get("run_id", active.run_id) == active.run_id
    )):
        scope = replace(active, contexts=contexts or active.contexts, **scope_fields)
    elif contexts is not None:
        scope = InvocationScope(contexts, **scope_fields)
    else:
        raise ValueError("A root invocation scope needs a ContextBuilder")
    if scope.task_id is None:
        scope.task_id = default_task_id
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


class LLMInvocationService:
    def __init__(self, contexts: ContextBuilderService | None = None) -> None:
        self.contexts = contexts

    def generate_text(self, request: ContextBuildRequest, instructions: str, *, operation: str) -> str:
        return self._call(request, instructions, operation=operation, method="text")

    def generate_json(
        self, request: ContextBuildRequest, instructions: str, *, operation: str,
        expected_shape: str, output_schema: dict | None = None, repair_json: bool = True,
    ) -> dict | list:
        return self._call(
            request, instructions, operation=operation, method="json", expected_shape=expected_shape,
            output_schema=output_schema, repair_json=repair_json,
        )

    def choose_tool(self, request: ContextBuildRequest, instructions: str, tools: list[dict], *, operation: str) -> dict:
        return self._call(request, instructions, operation=operation, method="tool", tools=tools)

    def check_connection(self, endpoint: ModelEndpointSettings) -> str:
        return self._call(
            ContextBuildRequest(Purpose.MODEL_HEALTH_CHECK), _HEALTH_PROMPT,
            operation="模型连接检查", method="health", endpoint=endpoint,
        )

    @staticmethod
    def _endpoint(purpose: Purpose) -> tuple[ModelEndpointSettings, bool]:
        settings = model_settings_service.effective()
        route = _PURPOSE_ROUTES.get(purpose, ModelRoute.MAIN)
        endpoint = settings.selector_endpoint() if route == ModelRoute.SELECTOR else settings.main_endpoint()
        official = endpoint.provider == "deepseek" and endpoint.base_url.rstrip("/") == DEEPSEEK_DESIGN_DATA_URL
        if route == ModelRoute.DEEPSEEK_DIRECT:
            if not official:
                raise RuntimeError("Design extraction is authorized only for https://api.deepseek.com")
            if not endpoint.ready:
                raise RuntimeError("The approved DeepSeek model is not enabled or configured")
        return endpoint, official and route in {ModelRoute.DEEPSEEK_DIRECT, ModelRoute.REFLECTION}

    def _call(
        self, request: ContextBuildRequest, instructions: str, *, operation: str, method: str,
        expected_shape: str | None = None, output_schema: dict | None = None,
        tools: list[dict] | None = None, repair_json: bool = False,
        endpoint: ModelEndpointSettings | None = None,
    ):
        from app.services.context_builder_service import context_builder_service

        scope = _scope.get()
        contexts = self.contexts or (scope.contexts if scope else None) or context_builder_service
        if scope and request.purpose != Purpose.MODEL_HEALTH_CHECK:
            fields = ("session_id", "run_id", "task_id", "generation", "agent_role", "source_message_id", "base_context_id")
            request = replace(request, **{
                field: getattr(scope, field) for field in fields if getattr(request, field) is None
            })
        endpoint, direct_only = (endpoint, False) if endpoint is not None else self._endpoint(request.purpose)
        if tools is not None and tuple(tool["function"]["name"] for tool in tools) != request.authorized_tool_names:
            raise ValueError("Function schemas must match the authorized shortlist")
        context = contexts.build_invocation(
            request, instructions=instructions, tools=tools, expected_shape=expected_shape, output_schema=output_schema,
            output_reserve=endpoint.max_output_tokens,
            expanded_reflection=request.purpose == Purpose.REFLECTION and direct_only,
        )
        provider = (
            provider_factory.get(endpoint, direct_only=direct_only)
            if direct_only or request.purpose == Purpose.REFLECTION else provider_factory.get(endpoint)
        )
        invocation_id = str(uuid4())
        try:
            return self._invoke(contexts, request, context, invocation_id, endpoint, instructions,
                                provider, operation, method, expected_shape, tools)
        except InvalidJSONOutput as error:
            if not repair_json:
                raise
            repair_request = replace(
                request, purpose=Purpose.JSON_REPAIR, source_message_id=None, base_context_id=None,
                query="", evidence_refs=(), authorized_tool_names=(),
                task_input={"raw_output": error.raw_output[:20_000], "expected_shape": output_schema or expected_shape},
            )
            repair = contexts.build_invocation(
                repair_request, instructions=_REPAIR_PROMPT, expected_shape=expected_shape,
                output_reserve=endpoint.max_output_tokens,
            )
            return self._invoke(contexts, repair_request, repair, str(uuid4()), endpoint, _REPAIR_PROMPT,
                                provider, "JSON 格式修复", "json", expected_shape, None, parent_id=invocation_id)

    @staticmethod
    def _invoke(contexts, request, context, invocation_id, endpoint, instructions, provider, operation,
                method, expected_shape, tools, *, parent_id=None):
        db = contexts.db
        with db.transaction() as conn:
            contexts.save_invocation_manifest(conn, request, context, invocation_id)
            conn.execute(
                """INSERT INTO llm_invocations
                (invocation_id, context_id, parent_invocation_id, session_id, run_id, task_id,
                 purpose, operation, provider, model, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (invocation_id, context.context_id, parent_id, request.session_id, request.run_id, request.task_id,
                 request.purpose, operation, endpoint.provider, endpoint.model, utc_now()),
            )
        scope = _scope.get()
        if (scope and request.session_id and request.session_id == scope.session_id
                and request.run_id is None and scope.contexts.db is db):
            scope.pending.append(invocation_id)
        usage = TokenUsage()
        token = token_usage_tracker.start_invocation(usage)
        started = perf_counter()
        status, error_code = "completed", None
        try:
            if method == "json":
                return provider.generate_json(
                    instructions, context.payload, operation=operation, expected_shape=expected_shape, repair_json=False,
                )
            if method == "tool":
                return provider.choose_tool(instructions, context.payload, tools)
            if method == "health":
                return provider.test_connection(instructions, context.payload["probe"])
            return provider.generate_text(instructions, db.dumps(context.payload))
        except BaseException as exc:
            status = "failed"
            error_code = exc.code if isinstance(exc, ModelInvocationError) else "INVOCATION_FAILED"
            raise
        finally:
            token_usage_tracker.finish_invocation(token)
            db.execute(
                """UPDATE llm_invocations SET status=?, input_tokens=?, output_tokens=?, total_tokens=?,
                   duration_ms=?, error_code=?, completed_at=? WHERE invocation_id=?""",
                (status, usage.input_tokens, usage.output_tokens, usage.total_tokens,
                 int((perf_counter() - started) * 1000), error_code, utc_now(), invocation_id),
            )


llm_invocation_service = LLMInvocationService()
