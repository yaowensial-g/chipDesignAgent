"""Exercise real Reflection routing, persisted budgets and deterministic acceptance."""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.agents.runtime import LLMChipDesignRuntime
from app.config.model_settings import ModelEndpointSettings
from app.domain.models import ChatRequest
from app.domain.orchestration import ArtifactPayload, EvidenceRef, LintResult, ReflectionResult, TaskContract
from app.services.task_verifier_service import TaskVerifierService
from app.tools.registry import tool_registry

from test_hierarchical_run import ScriptedRuntime, executor_factory, request_for, rtl_plan
from test_task_contracts import task_node


def _contract():
    return TaskContract.model_validate(task_node("source", "design", ("read_rtl",), ("rtl_snapshot",)))


@pytest.mark.parametrize(("decision", "tool_name"), [
    ("continue", "read_rtl_source"),
    ("submit", "submit_task_output"),
    ("replan", "report_task_failure"),
    ("ask_user", "ask_user"),
    ("fail", "report_task_failure"),
])
def test_reflection_and_selection_share_one_provider_call(monkeypatch, decision, tool_name):
    contract = _contract()
    runtime = LLMChipDesignRuntime()
    calls = []
    reflection, selection = ScriptedRuntime._reflection(contract, (), tool_name, {}, decision)

    class Provider:
        def choose_tool(self, prompt, payload, tools):
            calls.append((prompt, payload, tools))
            return {"name": tool_name, "arguments": {
                "reflection": reflection.model_dump(mode="json"), "tool_arguments": selection.arguments,
            }}

    monkeypatch.setattr(runtime, "_reflection_provider", lambda: (Provider(), True))
    context = {"budget": {"task": {"repair_remaining": 0}}, "replanning_enabled": True}
    result, picked = runtime.reflect_and_select(contract, context, [tool_registry.get(tool_name).definition])
    assert result == reflection and picked == selection
    [(prompt, payload, tools)] = calls
    assert "文件存在或非空不足以证明完成" in prompt
    assert "任务与 Run" in prompt and "验收条件" in prompt
    assert payload["context"]["budget"] == context["budget"]
    parameters = tools[0]["function"]["parameters"]
    assert parameters["required"] == ["reflection", "tool_arguments"]
    fields = parameters["properties"]["reflection"]["properties"]
    assert set(fields["decision"]["enum"]) == {
        "continue", "submit", "replan", "ask_user", "fail",
    }
    for field in ("satisfied_criteria", "unmet_criteria"):
        assert fields[field]["items"]["enum"] == ["deliver"] and fields[field]["uniqueItems"] is True


@pytest.mark.parametrize(("provider", "url", "approved"), [
    ("deepseek", "https://api.deepseek.com/", True),
    ("deepseek", "http://api.deepseek.com", False),
    ("deepseek", "https://api.deepseek.com.example.test", False),
    ("deepseek", "https://api.deepseek.com@elsewhere.example.test", False),
    ("openai", "https://api.deepseek.com", False),
])
def test_reflection_sends_expanded_evidence_only_to_approved_endpoint(monkeypatch, provider, url, approved):
    endpoint = ModelEndpointSettings(
        enabled=True, provider=provider, api_style="responses", model="synthetic",
        base_url=url, api_key="test-only", timeout_seconds=10,
        max_output_tokens=1000, api_key_source="test",
    )
    monkeypatch.setattr("app.agents.runtime.model_settings_service.effective",
                        lambda: SimpleNamespace(main_endpoint=lambda: endpoint))
    contract = _contract()
    name = "report_task_failure"
    reflection, selection = ScriptedRuntime._reflection(contract, (), name, {"reason": "Done"}, "fail")
    calls = []

    def choose_tool(prompt, payload, tools):
        calls.append(payload["context"])
        return {"name": name, "arguments": {
            "reflection": reflection.model_dump(mode="json"), "tool_arguments": selection.arguments,
        }}

    def get_provider(settings, *, direct_only):
        assert settings is endpoint and direct_only is approved
        return SimpleNamespace(choose_tool=choose_tool)

    monkeypatch.setattr("app.agents.runtime.provider_factory.get", get_provider)
    context = {
        "replanning_enabled": True,
        "budget": {"task": {"repair_remaining": 0}},
        "plan_tasks": [{"task_id": "source", "title": "Synthetic task"}],
        "artifacts": [{"artifact_id": "source", "rtl_source": {"excerpts": [{"text": "synthetic RTL"}]}}],
        "previous_attempt_feedback": {
            "failed_criteria": ["deliver"], "suggestions": ["legacy diagnostic"], "failure_reason": "expanded diagnostic",
        },
    }
    LLMChipDesignRuntime().reflect_and_select(contract, context, [tool_registry.get(name).definition])
    [sent] = calls
    assert ("rtl_source" in sent["artifacts"][0]) is approved
    assert ("failure_reason" in sent["previous_attempt_feedback"]) is approved
    assert sent["previous_attempt_feedback"]["suggestions"] == ["legacy diagnostic"]
    assert sent["budget"] == context["budget"] and sent["plan_tasks"] == context["plan_tasks"]
    assert context["artifacts"][0]["rtl_source"]["excerpts"][0]["text"] == "synthetic RTL"


@pytest.mark.asyncio
async def test_rtl_excerpt_and_unmet_semantics_block_submit(executor_factory):
    class BadRTL(ScriptedRuntime):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def reflect_and_select(self, contract, context, shortlist, evidence):
            if not context["artifacts"]:
                return super().reflect_and_select(contract, context, shortlist, evidence)
            [artifact] = context["artifacts"]
            source = artifact["rtl_source"]
            [snippet] = source["excerpts"]
            assert "assign tx = !rx;" in snippet["text"]
            assert (snippet["start_line"], snippet["end_line"]) == (1, 3)
            assert source["total_lines"] == 3 and not source["truncated"]
            assert source["relative_path"] == "rtl/uart.sv"
            assert source["content_hash"] == sha256(snippet["text"].encode()).hexdigest()
            self.reviews += 1
            decision, tool, arguments = "fail", "report_task_failure", {"reason": "RTL inverts the requested signal"}
            if self.reviews == 1:
                decision, tool, arguments = "submit", "submit_task_output", {"output_keys": ["rtl_snapshot"]}
            return ReflectionResult(
                progress="blocked", progress_summary="RTL implements opposite polarity", satisfied_criteria=(),
                unmet_criteria=("deliver",), issues=("tx should follow rx",), decision=decision,
                evidence=evidence, reason="Actual RTL assigns !rx",
            ), ScriptedRuntime._reflection(contract, evidence, tool, arguments, decision)[1]

    runtime = BadRTL()
    executor = executor_factory(runtime)
    request = request_for(executor).model_copy(update={"message": "Review that tx follows rx"})
    runtime.source_path.write_bytes(b"module uart(input rx, output tx);\nassign tx = !rx;\nendmodule\n")
    run = (await executor.handle(request)).run
    assert run.status.value == "failed" and run.result["message"] == "RTL inverts the requested signal"
    assert runtime.reviews == 2
    [source] = [task for task in executor.repo.states(run.run_id).values() if task.action_count]
    assert [item.status for item in executor.repo.observations(source.task_id)] == ["succeeded", "denied", "failed"]
    assert not executor.repo.verifications(source.task_id)
    assert not executor.repo.local_outputs(source.task_id, "verified")


@pytest.mark.asyncio
async def test_large_rtl_uses_relevant_line_ranges_and_shared_budget(executor_factory):
    class Capture(ScriptedRuntime):
        def reflect_and_select(self, contract, context, shortlist, evidence):
            if not context["artifacts"]:
                return super().reflect_and_select(contract, context, shortlist, evidence)
            excerpt = context["artifacts"][0]["rtl_source"]
            assert excerpt["truncated"] and excerpt["total_lines"] == 403
            assert any("peak_ready" in item["text"] and item["start_line"] <= 396 <= item["end_line"]
                       for item in excerpt["excerpts"])
            assert self.tokens.count(json.dumps(excerpt)) <= 1_800
            assert len(excerpt["excerpts"]) <= 3
            return self._reflection(contract, evidence, "report_task_failure", {"reason": "Checked sample"}, "fail")

    runtime = Capture()
    executor = executor_factory(runtime)
    runtime.tokens = executor.contexts.tokens
    request = request_for(executor).model_copy(update={"message": "Review peak_ready"})
    lines = ["module uart;", *[f"wire filler_{index};" for index in range(394)],
             "wire peak_ready = 1'b0;", *["// end"] * 6, "endmodule"]
    runtime.source_path.write_bytes("\n".join(lines).encode())
    run = (await executor.handle(request)).run
    assert run.result["message"] == "Checked sample"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["task", "run"])
async def test_last_decision_window_blocks_ordinary_action_and_prevents_next_model_call(executor_factory, monkeypatch, scope):
    class IgnoreBudget(ScriptedRuntime):
        decisions = 0

        def reflect_and_select(self, contract, context, shortlist, evidence):
            self.decisions += 1
            budget = context["budget"][scope]
            assert (budget["decision_used"], budget["decision_limit"], budget["decision_remaining"]) == (1, 1, 0)
            assert context["budget"]["task"]["action_used"] == 0
            assert {tool.name for tool in shortlist} == {"submit_task_output", "ask_user", "report_task_failure"}
            return self._reflection(contract, evidence, "read_rtl_source", {"relative_path": context["rtl_path"]}, "continue")

    runtime = IgnoreBudget()
    executor = executor_factory(runtime)
    if scope == "run":
        monkeypatch.setenv("CHIP_AGENT_MAX_RUN_DECISIONS", "1")
    else:
        def plan(payload):
            draft = rtl_plan(payload)
            next(task for task in draft["tasks"] if task["task_id"] == "design")["execution_policy"] = {"max_decisions": 1}
            return draft

        executor.planner.propose = plan

    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed" and "决策额度已耗尽" in run.result["message"]
    assert runtime.decisions == 1
    assert executor.repo.usage(run.run_id) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_replan", [True, False])
async def test_replan_without_evidence_or_repair_exhaustion_is_denied(executor_factory, monkeypatch, enable_replan):
    if enable_replan:
        monkeypatch.setenv("CHIP_AGENT_REPLANNING_ENABLED", "1")

    class InvalidReplan(ScriptedRuntime):
        def reflect_and_select(self, contract, context, shortlist, evidence):
            assert context["replanning_enabled"] is enable_replan
            decision = "fail" if context["observations"] else "replan"
            return self._reflection(contract, evidence, "report_task_failure", {"reason": "Stop the task"}, decision)

    executor = executor_factory(InvalidReplan())
    run = (await executor.handle(request_for(executor))).run
    active = next(task for task in executor.repo.states(run.run_id).values() if task.current_iteration)
    assert run.status.value == "failed" and not executor.repo.replan_history(run.run_id)
    assert [item.status for item in executor.repo.observations(active.task_id)] == ["denied", "failed"]
    assert [item.result.decision for item in executor.repo.reflections(active.task_id)] == ["fail"]


@pytest.mark.asyncio
async def test_permission_denial_ends_task_without_second_reflection_or_replan(executor_factory, monkeypatch):
    monkeypatch.setenv("CHIP_AGENT_REPLANNING_ENABLED", "1")
    executor = executor_factory(ScriptedRuntime())

    def denied(*args, **kwargs):
        raise PermissionError("test file access denied")

    monkeypatch.setattr(executor.actions.workspaces, "read_rtl_source", denied)
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "failed"
    assert executor.repo.usage(run.run_id) == (1, 1)
    assert not executor.repo.replan_history(run.run_id)
    source = next(task for task in executor.repo.states(run.run_id).values() if task.action_count)
    [observation] = executor.repo.observations(source.task_id)
    assert observation.error == "action_permission_denied"


@pytest.mark.asyncio
async def test_unchanged_inline_submission_is_rejected_and_real_repair_can_complete(executor_factory, monkeypatch):
    monkeypatch.setattr("app.agents.runtime.runtime.select_tool_names", lambda *_: pytest.fail("Hierarchical selector must be local"))

    class RepairReport(ScriptedRuntime):
        def reflect_and_select(self, contract, context, shortlist, evidence):
            budget = context["budget"]["task"]
            key = contract.outputs[0].key
            text = "   " if budget["decision_used"] <= 2 else "Repaired report."
            if budget["decision_used"] >= 2:
                assert context["verification_feedback"]["failed_criteria"] == [f"invalid:{key}"]
                assert budget["repair_used"] == 0
            return self._reflection(contract, evidence, "submit_task_output", {
                "output_keys": [key], "text_outputs": {key: text},
            }, "submit")

    executor = executor_factory(RepairReport())
    run = (await executor.handle(ChatRequest(
        project_id="chip", module_name="uart", message="Explain UART", orchestration_mode="chat",
    ))).run
    assert run.status.value == "completed"
    [state] = executor.repo.states(run.run_id).values()
    assert state.repair_attempts == 1 and executor.repo.usage(run.run_id) == (0, 3)
    assert [entry["result"]["passed"] for entry in executor.repo.verifications(state.task_id) if entry["scope"] == "task"] == [False, True]
    assert any(item.error == "unchanged_submission" for item in executor.repo.observations(state.task_id))
    assert len(executor.repo.artifact_history(state.task_id, state.generation)) == 2


@pytest.mark.asyncio
async def test_historical_reflections_with_old_submit_and_fail_are_readable(executor_factory):
    executor = executor_factory(ScriptedRuntime())
    run = (await executor.handle(request_for(executor))).run
    assert run.status.value == "completed"
    source = next(task for task in executor.repo.plans(run.run_id)[0].tasks if task.title == "design")
    records = executor.repo.reflections(source.task_id)
    old_submit = records[-1].result.model_dump(mode="json")
    old_submit["unmet_criteria"], old_submit["satisfied_criteria"] = old_submit["satisfied_criteria"], []
    for old, decision in [(records[0], "fail"), (records[-1], "submit")]:
        value = old.model_dump(mode="json")
        value["result"] = {**old_submit, "decision": decision}
        executor.repo.db.execute(
            "UPDATE orchestration_actions SET reflection_json=? WHERE action_id=?", (json.dumps(value), old.action_id),
        )
    restored = executor.repo.reflections(source.task_id)
    assert restored[0].result.decision == "fail"
    assert restored[-1].result.decision == "submit" and restored[-1].result.unmet_criteria == ("deliver",)


@pytest.mark.asyncio
async def test_verifier_rejects_empty_wrong_hash_and_unlinked_lint(executor_factory):
    executor = executor_factory(ScriptedRuntime())
    run = (await executor.handle(request_for(executor))).run
    plan = executor.repo.plans(run.run_id)[0]
    contract = next(task for task in plan.tasks if task.title == "design")
    [snapshot] = [item for item in executor.repo.local_outputs(contract.task_id, "verified") if item.artifact.artifact_type == "rtl_snapshot"]
    verifier = TaskVerifierService(executor.runs.tools)
    for content in (b"", b"valid-looking RTL with incorrect content hash"):
        staged = ArtifactPayload(artifact=snapshot.artifact.model_copy(update={"status": "staged"}), content=content)
        result = verifier.verify(contract, (staged,), (), contract.acceptance_criteria)
        assert not result.passed and "invalid:rtl_snapshot" in result.failed_criteria

    lint = TaskContract.model_validate(task_node("lint", "design", ("real_rtl_lint",), ("lint_result",)))
    receipt = LintResult(
        tool_run_id="missing-tool-run", snapshot_ref=snapshot.artifact.artifact_id, content_hash=snapshot.artifact.content_hash,
        top_module="uart", version="Verilator", exit_code=0, stdout_hash=sha256(b"").hexdigest(), stderr_hash=sha256(b"").hexdigest(),
    )
    state = executor.repo.states(run.run_id)[contract.task_id].model_copy(update={"task_id": lint.task_id})
    candidate = executor.repo.staged_payload(state, lint, "unlinked-action", "lint_result", receipt.model_dump_json().encode(), {})
    result = verifier.verify(lint, (candidate,), (snapshot,), lint.acceptance_criteria)
    assert not result.passed and "invalid:lint_result" in result.failed_criteria
