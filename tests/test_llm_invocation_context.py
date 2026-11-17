import sqlite3
from types import SimpleNamespace

import pytest

from app.agents.runtime import LLMChipDesignRuntime
from app.config.model_settings import ModelEndpointSettings
from app.domain.context import ContextBuildRequest, ContextPurpose
from app.domain.orchestration import EvidenceRef, TaskContract
from app.models.provider import InvalidJSONOutput
from app.services.context_builder_service import ContextBudgetConfig, ContextBudgetExceeded, ContextBuilderService
from app.services.context_profiles import CONTEXT_PROFILES
from app.services.llm_invocation_service import LLMInvocationService, invocation_scope
from app.services.session_service import SessionService
from app.services.token_usage_service import token_usage_tracker
from app.storage.database import Database
from app.tools.registry import tool_registry

from test_hierarchical_run import ReplanningRuntime, executor_factory, request_for, rtl_plan


def test_profiles_and_budget(tmp_path):
    assert CONTEXT_PROFILES.keys() == set(ContextPurpose)
    contexts = ContextBuilderService(Database(tmp_path / "db.sqlite"), budget_config=ContextBudgetConfig(
        total=300, output_reserve=20,
    ))
    request = ContextBuildRequest(ContextPurpose.CHAT_RESPONSE, task_input={
        "current_message": "RTL help", "documents": [{"content": "document " * 500}], "private": "unused",
    })
    context = contexts.build_invocation(request, instructions="respond")
    assert context.payload["current_message"] == "RTL help"
    assert context.payload["documents"] == []
    assert {item["category"] for item in context.excluded_sources} == {"documents", "private"}
    with pytest.raises(ContextBudgetExceeded):
        contexts.build_invocation(request, instructions="instruction " * 500)
    with pytest.raises(ContextBudgetExceeded):
        contexts.build_invocation(request, tools=[{"name": "test", "description": "schema " * 500}])


def test_json_repair_is_an_independently_audited_call(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    service = LLMInvocationService(ContextBuilderService(db))
    endpoint = ModelEndpointSettings(True, "openai", "responses", "test", "https://example.test", "private", 1, 200, "env")
    monkeypatch.setattr(service, "_endpoint", lambda purpose: (endpoint, False))
    received = []

    def generate(instructions, payload, **kwargs):
        received.append((instructions, payload))
        assert kwargs["repair_json"] is False
        token_usage_tracker.record_response(SimpleNamespace(usage={"input_tokens": 5, "output_tokens": 2}))
        if len(received) == 1:
            raise InvalidJSONOutput("test", "broken output")
        return {"summary": "ok"}

    monkeypatch.setattr("app.services.llm_invocation_service.provider_factory.get",
                        lambda endpoint: SimpleNamespace(generate_json=generate))
    token = token_usage_tracker.start()
    try:
        request = ContextBuildRequest(ContextPurpose.SUMMARY_COMPRESSION, task_input={
            "summary": {"topic": "secret context"}, "source_message_ids": [],
        })
        assert service.generate_json(request, "secret instructions", operation="test", expected_shape="{}") == {"summary": "ok"}
    finally:
        usage = token_usage_tracker.finish(token)
    assert usage.model_calls == 2
    assert usage.total_tokens == 14
    assert received[1][1] == {"raw_output": "broken output", "expected_shape": "{}"}
    calls = db.fetch_all("SELECT * FROM llm_invocations ORDER BY rowid")
    assert [call["status"] for call in calls] == ["failed", "completed"]
    assert calls[1]["parent_invocation_id"] == calls[0]["invocation_id"]
    assert [call["total_tokens"] for call in calls] == [7, 7]
    manifests = db.fetch_all("SELECT * FROM context_manifests ORDER BY rowid")
    assert [item["purpose"] for item in manifests] == ["SUMMARY_COMPRESSION", "JSON_REPAIR"]
    assert [item["invocation_id"] for item in manifests] == [call["invocation_id"] for call in calls]
    assert "secret" not in str(manifests)


def test_selector_and_expert_use_only_scoped_sources(tmp_path):
    contexts = ContextBuilderService(Database(tmp_path / "db.sqlite"))
    selected = {
        "project_id": "chip", "module_name": "uart", "message": "Analyze UART",
        "history": [{"role": "user", "content": "private conversation"}],
        "_memory_context": [{"memory_id": "private", "content": "private memory"}],
        "_project_config": {"revision": "current"},
        "documents": [{"name": "spec", "content": "authorized document"}],
    }
    selector = contexts.build_invocation(ContextBuildRequest(
        ContextPurpose.TOOL_SELECTOR, authorized_tool_names=("ask_user",),
        task_input={
            "request": selected, "intent": {"goal": "Analyze UART"}, "max_tools": 1,
            "observations": [{"result": "private result"}],
            "tool_metadata": [{"name": "ask_user", "input_schema": {"private": "x"}}, {"name": "unauthorized"}],
        },
    ))
    assert selector.payload["authorized_tool_metadata"] == [{"name": "ask_user"}]
    assert selector.payload["has_observations"]
    assert "private" not in str(selector.payload)
    expert = contexts.build_invocation(ContextBuildRequest(
        ContextPurpose.SPECIALIST_ANALYSIS, task_input={"request": selected},
    ))
    assert expert.payload["history"] == [] and expert.payload["documents"] == selected["documents"]
    assert "private" not in str(expert.payload) and "project_configuration" not in expert.payload


@pytest.mark.parametrize(("purpose", "inputs", "required", "disallowed"), [
    (ContextPurpose.CHAT_BASE, {}, "current_message", "ds_markdown"),
    (ContextPurpose.CHAT_RESPONSE, {}, "current_message", "ds_markdown"),
    (ContextPurpose.INTENT_ENRICHMENT, {}, "raw_request", "ds_markdown"),
    (ContextPurpose.TOOL_CALL, {"intent": {}, "observations": [{"tool_name": "read"}]}, "previous_observations", "ds_markdown"),
    (ContextPurpose.DS_GENERATION, {"design_analysis": {}, "user_resolution": None}, "design_analysis", "history"),
    (ContextPurpose.RTL_GENERATION, {"ds_markdown": "# DS"}, "ds_markdown", "history"),
    (ContextPurpose.INDEPENDENT_REVIEW, {"ds_markdown": "# DS", "review_category": "Interface", "custom_prompt": None},
     "review_category", "long_term_memory"),
])
def test_request_profiles_project_selected_base_sources(tmp_path, purpose, inputs, required, disallowed):
    contexts = ContextBuilderService(Database(tmp_path / "db.sqlite"))
    request = {
        "project_id": "chip", "module_name": "uart", "message": "Analyze UART",
        "_project_config": {"revision": 7}, "_memory_context": [{"memory_id": "memory", "content": "experience"}],
        "history": [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}],
        "_context_manifest": {"raw_turn_ids": ["prior-turn"], "message_ids": ["prior-user", "prior-answer"]},
    }
    result = contexts.build_invocation(ContextBuildRequest(purpose, task_input={"request": request, **inputs}))
    assert result.payload["project_configuration"]["revision"] == 7
    assert required in result.payload and disallowed not in result.payload
    if "history" in result.payload:
        assert result.payload["history"] == request["history"]
        assert result.included_sources["raw_turn_ids"] == ["prior-turn"]
        assert result.included_sources["message_ids"] == ["prior-user", "prior-answer"]


def test_summary_and_probe_are_isolated_and_manifest_migration_preserves_legacy_rows(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE context_manifests (
                context_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT, agent_role TEXT NOT NULL,
                token_budget INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL, message_ids_json TEXT NOT NULL,
                memory_ids_json TEXT NOT NULL, chunk_ids_json TEXT NOT NULL, observation_ids_json TEXT NOT NULL,
                sections_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO context_manifests VALUES
                ('legacy', 'legacy-session', 'legacy-run', 'design', 100, 50, '[]', '[]', '[]', '[]', '{}', 'old');
        """)
    db = Database(path)
    contexts = ContextBuilderService(db)
    assert contexts.get_manifest("legacy").session_id == "legacy-session"
    turn = SimpleNamespace(
        turn_id="complete", user=SimpleNamespace(message_id="u", content="question"),
        assistant=SimpleNamespace(message_id="a", content="answer"),
    )
    summary = contexts.build_invocation(ContextBuildRequest(
        ContextPurpose.CONVERSATION_SUMMARY, task_input={"turn": turn, "history": "unrelated"},
    ))
    assert set(summary.payload) == {"turn_id", "user", "assistant"}
    assert summary.included_sources["message_ids"] == ["u", "a"]
    received = []
    monkeypatch.setattr("app.services.llm_invocation_service.provider_factory.get", lambda endpoint: SimpleNamespace(
        test_connection=lambda instructions, probe: received.append(probe) or "CHIP_AGENT_CONNECTION_OK",
    ))
    endpoint = ModelEndpointSettings(True, "openai", "responses", "test", "https://example.test", "private", 1, 200, "env")
    LLMInvocationService(contexts).check_connection(endpoint)
    assert received == ["只回答 CHIP_AGENT_CONNECTION_OK"]
    [call] = db.fetch_all("SELECT context_id, session_id FROM llm_invocations")
    assert call["session_id"] is None
    assert contexts.get_manifest(call["context_id"]).purpose == "MODEL_HEALTH_CHECK"


def test_prerun_manifest_is_linked_to_its_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    sessions = SessionService(db)
    sessions.resolve("session", "chip", None, "uart")
    contexts = ContextBuilderService(db, sessions=sessions)
    endpoint = ModelEndpointSettings(True, "openai", "responses", "test", "https://example.test", "private", 1, 200, "env")
    service = LLMInvocationService(contexts)
    monkeypatch.setattr(service, "_endpoint", lambda purpose: (endpoint, False))
    monkeypatch.setattr("app.services.llm_invocation_service.provider_factory.get",
                        lambda endpoint: SimpleNamespace(generate_json=lambda *args, **kwargs: {}))
    with invocation_scope(contexts, session_id="session", agent_role="design") as scope:
        request = ContextBuildRequest(
            ContextPurpose.INTENT_ENRICHMENT, task_input={"request": {"message": "Analyze UART"}},
        )
        service.generate_json(request, "Intent", operation="Intent", expected_shape="{}")
        [draft] = db.fetch_all("SELECT run_id, session_id FROM llm_invocations")
        assert draft == {"run_id": None, "session_id": "session"}
        scope.attach_run("run", "base")
    [manifest] = contexts.list_for_run("run")
    assert manifest.base_context_id == "base"
    assert db.fetch_one("SELECT run_id FROM llm_invocations WHERE invocation_id=?", (manifest.invocation_id,))["run_id"] == "run"


@pytest.mark.asyncio
async def test_hierarchical_model_calls_reuse_base_and_refresh_dynamic_evidence(executor_factory, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_RUNTIME_MODE", "production")
    endpoint = ModelEndpointSettings(
        True, "deepseek", "chat_completions", "test", "https://api.deepseek.com", "synthetic", 5, 200, "env",
    )
    monkeypatch.setattr("app.services.llm_invocation_service.model_settings_service.effective",
                        lambda: SimpleNamespace(ready=True, max_output_tokens=200, main_endpoint=lambda: endpoint))
    script = ReplanningRuntime(repair=True)
    seen = {}

    class Provider:
        def generate_json(self, instructions, payload, *, operation, **kwargs):
            token_usage_tracker.record_response(SimpleNamespace(usage={"input_tokens": 5, "output_tokens": 1}))
            if operation == "任务意图完善 Agent":
                return {"goal": payload["raw_request"], "confidence": 10}
            if operation == "全局任务规划":
                assert payload["context"]["workflow_contract"]["rtl_path"] == "rtl/uart.sv"
                return rtl_plan(payload)
            if operation == "RTL 设计分析":
                return script.analyze_rtl_design(payload)
            if operation == "RTL 验证专家评估":
                return script.assess_rtl_verification(payload)
            pytest.fail(f"Unexpected invocation: {operation}")

        def choose_tool(self, instructions, payload, tools):
            token_usage_tracker.record_response(SimpleNamespace(usage={"input_tokens": 5, "output_tokens": 1}))
            contract = TaskContract.model_validate(payload["contract"])
            context = payload["context"]
            seen.setdefault(contract.title, []).append(context)
            evidence = tuple(EvidenceRef.model_validate(item) for item in payload["visible_evidence"])
            shortlist = [tool_registry.get(item["function"]["name"]).definition for item in tools]
            reflection, selection = script.reflect_and_select(contract, context, shortlist, evidence)
            return {"name": selection.name, "arguments": {
                "reflection": reflection.model_dump(mode="json"), "tool_arguments": selection.arguments,
            }}

    monkeypatch.setattr("app.services.llm_invocation_service.provider_factory.get", lambda endpoint, **kwargs: Provider())
    executor = executor_factory(LLMChipDesignRuntime())
    executor.planner.propose = None
    request = request_for(executor)
    token = token_usage_tracker.start()
    try:
        run = (await executor.handle(request)).run
    finally:
        usage = token_usage_tracker.finish(token)
    assert run.status.value == "completed", run.result
    assert not seen["design"][0]["observations"]
    assert seen["design"][1]["observations"][0]["tool_name"] == "read_rtl_source"
    assert "rtl_source" in seen["design"][1]["artifacts"][0]
    assert seen["design"][1]["budget"]["task"]["decision_remaining"] < seen["design"][0]["budget"]["task"]["decision_remaining"]
    assert seen["verify"][1]["verification_feedback"]["passed"] is False
    calls = executor.repo.db.fetch_all("SELECT * FROM llm_invocations WHERE run_id=? ORDER BY rowid", (run.run_id,))
    manifests = executor.contexts.list_for_run(run.run_id)
    base, *invocations = manifests
    assert base.purpose == "CHAT_BASE"
    assert len(invocations) == usage.model_calls == len(calls)
    assert sum(item["total_tokens"] for item in calls) == usage.total_tokens == 6 * len(calls)
    assert {item.purpose for item in invocations} == {
        "INTENT_ENRICHMENT", "GLOBAL_PLANNING", "REFLECTION", "SPECIALIST_ANALYSIS", "VERIFICATION",
    }
    assert all(item.base_context_id == base.context_id for item in invocations if item.agent_role == "design")
    assert all(item.base_context_id is None for item in invocations if item.agent_role == "verification")
    assert all(item.orchestration_task_id for item in invocations if item.purpose == "REFLECTION")
    assert any(item.artifact_ids and item.observation_ids for item in invocations if item.purpose == "REFLECTION")
