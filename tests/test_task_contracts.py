import copy
from types import SimpleNamespace

import pytest

from app.agents.runtime import LLMChipDesignRuntime
from app.domain.enums import AgentRole, Mutability
from app.domain.models import ChatRequest, TaskIntent
from app.domain.orchestration import EvidenceRef, ReflectionResult, TaskActionResult, TaskConstraints, TaskContract
from app.models.provider import ModelInvocationError
from app.services.global_planner_service import GlobalPlannerService, PlanningPolicy
from app.tools.builtin import register_builtin_tools
from app.tools.orchestration import register_orchestration_tools
from app.tools.registry import tool_registry


def task_node(key, role, capabilities, output_keys, dependencies=(), inputs=()):
    return {
        "task_id": key, "title": key, "goal": key, "assigned_agent": role,
        "capability_requirements": capabilities, "inputs": inputs, "depends_on": dependencies,
        "outputs": [
            {"key": output, "artifact_type": output, "media_type": "application/json"}
            for output in output_keys
        ],
        "constraints": {"read_only": True},
        "acceptance_criteria": [
            {"criterion_id": "deliver", "description": "Output present", "rule_id": "required_output", "output_keys": output_keys},
        ],
    }


def upstream(parent, key):
    return {
        "key": key, "artifact_type": key,
        "source": {"kind": "task_output", "task_id": parent, "output_key": key},
    }


@pytest.fixture
def draft():
    report = task_node("report", "design", ("compose_report",), ("report",), ("rtl", "verify"), (
        upstream("rtl", "design_analysis"), upstream("verify", "verification_assessment"),
    ))
    return {
        "goal": "Analyze UART",
        "tasks": [
            report,
            task_node("verify", "verification", ("rtl_verification_assessment",), ("verification_assessment",),
                      ("rtl",), (upstream("rtl", "rtl_snapshot"), upstream("rtl", "design_analysis"))),
            task_node("rtl", "design", ("read_rtl", "rtl_design_analysis"), ("rtl_snapshot", "design_analysis")),
        ],
        "final_task_id": "report",
        "success_criteria": report["acceptance_criteria"],
    }


@pytest.fixture
def policy():
    register_orchestration_tools()
    register_builtin_tools()
    return PlanningPolicy(
        primary_agent=AgentRole.DESIGN,
        allowed_tools={
            AgentRole.DESIGN: ("read_rtl_source", "analyze_rtl_design", "submit_task_output"),
            AgentRole.VERIFICATION: ("assess_rtl_verification",),
        },
        user_constraint_ids=("read_only_uart",),
    )


def plan(propose, policy, *, simple=False):
    request = ChatRequest(project_id="chip", module_name="uart", message="Analyze UART")
    intent = TaskIntent(raw_request=request.message, goal=request.message)
    return GlobalPlannerService(propose=propose).plan(
        "run-stage-a", request, intent, {"history": ["prior message"], "source": "actual content"}, policy,
        simple_question=simple,
    )


def test_planner_receives_context_and_remaps_dependencies(draft, policy):
    def propose(payload):
        assert payload["context"]["source"] == "actual content"
        assert "input_schema" not in payload["catalog"]["design"]["read_rtl_source"]
        return draft

    result = plan(propose, policy)
    assert result.status == "planned"
    final, verify, rtl = result.plan.tasks
    assert final.task_id == result.plan.final_task_id != "report"
    assert verify.depends_on == (rtl.task_id,)
    assert verify.inputs[0].source.task_id == rtl.task_id
    assert rtl.constraints.user_constraint_ids == ("read_only_uart",)


def test_planner_repairs_once_and_preserves_safe_diagnostics(draft, policy):
    calls = []

    def propose(payload):
        calls.append(payload["validation_feedback"])
        if len(calls) == 1:
            raise ModelInvocationError("provider output contains private details")
        return draft

    assert plan(propose, policy).status == "planned"
    assert calls == [[], ["planner_output_unavailable"]]
    result = plan(lambda _: {"internal_provider_response": "private"}, policy)
    assert result.status == "failed"
    assert result.plan is None and len(result.diagnostics) == 2
    assert "private" not in result.message and "run-stage-a" in result.message


@pytest.mark.parametrize("problem,code", [
    ("cycle", "dependency_cycle"), ("limit", "too_many_tasks"),
    ("tool", "unauthorized_capability"), ("rule", "invalid_acceptance_rule"),
    ("input", "invalid_input_source"),
])
def test_invalid_plan_is_never_activated(draft, policy, problem, code):
    if problem == "cycle":
        draft["tasks"][2]["depends_on"] = ("report",)
    elif problem == "limit":
        for index in range(6):
            node = copy.deepcopy(draft["tasks"][2])
            node["task_id"] = f"extra-{index}"
            draft["tasks"].append(node)
    elif problem == "tool":
        draft["tasks"][1]["capability_requirements"] = ("produce_rtl_candidate",)
    elif problem == "rule":
        draft["tasks"][0]["acceptance_criteria"][0]["rule_id"] = "unknown_rule"
    elif problem == "input":
        draft["tasks"][1]["inputs"][0]["source"]["output_key"] = "unknown_output"
    result = plan(lambda _: draft, policy)
    assert result.status == "failed" and result.plan is None
    assert result.diagnostics == (code, code)


def test_single_task_and_environment_checks_do_not_call_planner(policy):
    def no_model(_):
        pytest.fail("This case should not call the planner")

    simple = plan(no_model, policy, simple=True)
    assert len(simple.plan.tasks) == 1
    assert simple.plan.tasks[0].execution_policy.max_action_steps == 0
    assert plan(no_model, policy.model_copy(update={"required_inputs": ("rtl",)})).status == "waiting_for_user"
    assert plan(no_model, policy.model_copy(update={"required_tools": ("verilator_lint",)})).status == "capability_gap"


def test_unconfigured_planning_model_is_a_capability_gap(policy):
    def unavailable(_):
        raise ModelInvocationError("Missing provider configuration", code="MODEL_NOT_CONFIGURED")

    result = plan(unavailable, policy)
    assert result.status == "capability_gap"
    assert result.diagnostics == ("planning_model_unavailable",)


def test_combined_reflection_selection_checks_evidence_and_decision(monkeypatch, draft):
    contract = TaskContract.model_validate(draft["tasks"][2])
    evidence = EvidenceRef(kind="observation", ref_id="read-result")
    reflection = {
        "progress": "in_progress", "progress_summary": "Read RTL",
        "satisfied_criteria": [], "unmet_criteria": ["deliver"], "issues": [],
        "decision": "continue", "reason": "Analyze snapshot",
        "evidence": [evidence.model_dump(mode="json")],
    }
    calls = []

    def choose_tool(_, payload, schemas):
        calls.append(payload)
        assert "reflection" in schemas[0]["function"]["parameters"]["properties"]
        return {
            "name": "analyze_rtl_design",
            "arguments": {"reflection": reflection, "tool_arguments": {"snapshot_ref": "bound-source"}},
        }

    agent = LLMChipDesignRuntime()
    monkeypatch.setattr(agent, "_provider", lambda: SimpleNamespace(choose_tool=choose_tool))
    shortlist = [tool_registry.get("analyze_rtl_design").definition]
    parsed, selection = agent.reflect_and_select(contract, {"observations": ["read"]}, shortlist, (evidence,))
    assert isinstance(parsed, ReflectionResult) and selection.name == "analyze_rtl_design" and len(calls) == 1
    with pytest.raises(ValueError, match="unavailable evidence"):
        agent.reflect_and_select(contract, {}, shortlist)
    reflection["decision"] = "submit"
    with pytest.raises(ValueError, match="does not match"):
        agent.reflect_and_select(contract, {}, shortlist, (evidence,))
    reflection["decision"] = "continue"
    reflection["satisfied_criteria"] = ["deliver"]
    with pytest.raises(ValueError, match="classify each"):
        agent.reflect_and_select(contract, {}, shortlist, (evidence,))


def test_read_only_contract_rejects_candidate_write():
    with pytest.raises(ValueError, match="Read-only"):
        TaskConstraints(allowed_mutability=(Mutability.CANDIDATE_WRITE,))


def test_action_result_requires_a_consistent_outcome():
    with pytest.raises(ValueError, match="matching wait"):
        TaskActionResult(outcome="waiting_for_approval", summary="Approval required")
    with pytest.raises(ValueError, match="professional object"):
        TaskActionResult(outcome="awaiting_external", summary="ToolRun started")
