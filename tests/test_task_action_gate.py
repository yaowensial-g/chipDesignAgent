import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ChatRequest, ToolSelection, WorkspaceCreateRequest
from app.domain.orchestration import (
    ActionApproval,
    Artifact,
    ArtifactPayload,
    AuthorizedAction,
    ContentRef,
    TaskActionContext,
    TaskContract,
)
from app.services.delegation_service import DelegationService
from app.services.orchestrator_service import orchestrator_service
from app.services.run_service import RunService
from app.services.task_action_service import TaskActionService
from app.services.tool_selector_service import tool_selector_service
from app.services.workspace_service import WorkspaceService
from app.storage.database import Database
from app.tools.registry import ToolRegistry, tool_registry

from test_task_contracts import task_node, upstream


@pytest.fixture
def uart(tmp_path):
    db = Database(tmp_path / "stage-a.db")
    workspaces = WorkspaceService(db, tmp_path / "workspaces")
    workspace = workspaces.create(WorkspaceCreateRequest(project_id="chip"))
    content = (Path(__file__).parent / "fixtures" / "uart.sv").read_bytes()
    workspaces.resolve_path(workspace.workspace_id, "rtl/uart.sv").write_bytes(content)
    request = ChatRequest(project_id="chip", module_name="uart", message="Analyze UART")
    run_id = RunService(db=db, workspaces=workspaces).create_orchestration_run(
        "session-stage-a", request, {}, AgentRole.DESIGN,
    )
    context = TaskActionContext(
        run_id=run_id, generation=1, action_id="read", primary_agent=AgentRole.DESIGN,
        project_id="chip", module_name="uart", workspace_id=workspace.workspace_id,
        permitted_tools=("read_rtl_source", "analyze_rtl_design"),
        allowed_rtl_paths=("rtl/uart.sv",),
        contract=TaskContract.model_validate(task_node(
            "design", "design", ("read_rtl", "rtl_design_analysis"), ("rtl_snapshot", "design_analysis"),
        )),
    )
    return db, workspaces, context, content


def stage_output(context, stored):
    def stage(output_key, content, metadata):
        spec = next(item for item in context.contract.outputs if item.key == output_key)
        digest = hashlib.sha256(content).hexdigest()
        artifact = Artifact(
            artifact_id=str(uuid4()), run_id=context.run_id, name=spec.key,
            artifact_type=spec.artifact_type, source_task_id=context.contract.task_id,
            source_generation=context.generation, source_action_id=context.action_id,
            output_key=spec.key, version=1, content_hash=digest, status="staged", metadata=metadata,
            content_ref=ContentRef(
                backend="managed_blob", object_key=digest, media_type=spec.media_type, byte_size=len(content),
            ),
            created_at=datetime.now(timezone.utc),
        )
        stored[artifact.artifact_id] = ArtifactPayload(artifact=artifact, content=content)
        return artifact

    return stage


async def dispatch(service, context, name, arguments, stored):
    selection = ToolSelection(name=name, arguments=arguments)
    action = service.authorize_task_action(context, selection, [service.registry.get(name).definition])
    assert isinstance(action, AuthorizedAction)
    return await service.execute_task_action(context, action, stage_output(context, stored))


@pytest.mark.asyncio
async def test_uart_uses_real_source_bytes_and_verified_upstream_inputs(uart):
    db, workspaces, context, content = uart
    stored = {}
    model_calls = []

    def finding(payload):
        source = payload["snapshot"]
        return {
            "statement": "Reset makes the serial output idle",
            "evidence": [{
                "snapshot_ref": source["content_ref"], "content_hash": source["content_hash"],
                "start_line": 12, "end_line": 18,
            }],
        }

    def analyze(payload):
        assert payload["source_text"] == content.decode()
        model_calls.append("design")
        return {"summary": "Design review", "findings": [finding(payload)]}

    def assess(payload):
        assert payload["source_text"] == content.decode()
        assert payload["design_analysis"]["summary"] == "Design review"
        assert "history" not in payload and "documents" not in payload and "workspace_id" not in payload
        model_calls.append("verification")
        return {
            "summary": "Verification review",
            "impacts": [finding(payload)],
            "recommended_checks": [finding(payload)],
        }

    memories = SimpleNamespace(retrieve=lambda *args, **kwargs: [])
    extractor = SimpleNamespace(extract_from_event=lambda **kwargs: None)
    service = TaskActionService(
        workspaces=workspaces, delegations=DelegationService(db, memories, extractor),
        agent_runtime=SimpleNamespace(analyze_rtl_design=analyze, assess_rtl_verification=assess),
    )
    read = await dispatch(service, context, "read_rtl_source", {"relative_path": "rtl/uart.sv"}, stored)
    snapshot_id, = read.candidate_artifact_ids
    snapshot = stored[snapshot_id]
    assert snapshot.content == content and snapshot.artifact.content_hash == hashlib.sha256(content).hexdigest()
    context = context.model_copy(update={"action_id": "analysis", "inputs": (snapshot,)})
    designed = await dispatch(service, context, "analyze_rtl_design", {"snapshot_ref": snapshot_id}, stored)
    design_id, = designed.candidate_artifact_ids
    assessment_context = context.model_copy(update={
        "contract": TaskContract.model_validate(task_node(
            "verify", "verification", ("rtl_verification_assessment",), ("verification_assessment",), ("design",),
            (upstream("design", "rtl_snapshot"), upstream("design", "design_analysis")),
        )),
        "permitted_tools": ("assess_rtl_verification",),
        "action_id": "verify",
        "inputs": (snapshot, stored[design_id]),
    })
    args = {"snapshot_ref": snapshot_id, "design_ref": design_id}
    denied = await dispatch(service, assessment_context, "assess_rtl_verification", args, stored)
    assert denied.outcome == "failed" and model_calls == ["design"]

    verified_inputs = tuple(
        item.model_copy(update={"artifact": item.artifact.model_copy(update={"status": "verified"})})
        for item in assessment_context.inputs
    )
    assessment_context = assessment_context.model_copy(update={"inputs": verified_inputs})
    success = await dispatch(service, assessment_context, "assess_rtl_verification", args, stored)
    assert success.outcome == "observed" and model_calls == ["design", "verification"]
    assert success.professional_refs[0].kind == "delegation"
    assert stored[success.candidate_artifact_ids[0]].artifact.status == "staged"
    wrong = verified_inputs[0].model_copy(update={"content": b"changed source"})
    corrupted = assessment_context.model_copy(update={"inputs": (wrong, verified_inputs[1])})
    denied = await dispatch(service, corrupted, "assess_rtl_verification", args, stored)
    assert denied.outcome == "failed" and model_calls == ["design", "verification"]


def test_task_shortlist_and_gate_enforce_scope(uart):
    _, workspaces, context, _ = uart
    service = TaskActionService(workspaces=workspaces)
    read = ToolSelection(name="read_rtl_source", arguments={"relative_path": "rtl/uart.sv"})
    definition = tool_registry.get(read.name).definition
    assert orchestrator_service._tool_context([]) == []
    assert read.name not in {tool.name for tool in tool_registry.for_role(AgentRole.DESIGN)}
    assert read.name not in {tool.name for tool in tool_registry.list()}
    with pytest.raises(PermissionError):
        tool_registry.authorize(read.name, AgentRole.DESIGN)
    tools, _ = tool_selector_service.select_for_task(context.contract, {}, permitted_tools=context.permitted_tools)
    assert read.name in {tool.name for tool in tools}
    controls, _ = tool_selector_service.select_for_task(
        context.contract, {}, permitted_tools=context.permitted_tools, controls_only=True,
    )
    assert {tool.name for tool in controls} == {"ask_user", "submit_task_output", "report_task_failure"}
    assert service.authorize_task_action(context, read, []).error_code == "tool_not_shortlisted"
    assert service.authorize_task_action(
        context.model_copy(update={"project_id": "other"}), read, [definition],
    ).error_code == "workspace_scope_denied"
    invalid = read.model_copy(update={"arguments": {**read.arguments, "workspace_id": "other"}})
    assert service.authorize_task_action(context, invalid, [definition]).error_code == "invalid_tool_arguments"
    invalid = read.model_copy(update={"arguments": {"relative_path": "rtl/other.sv"}})
    assert service.authorize_task_action(context, invalid, [definition]).error_code == "rtl_path_scope_denied"
    assert service.authorize_task_action(
        context.model_copy(update={"action_count": 8}), read, [definition],
    ).error_code == "action_budget"


@pytest.mark.asyncio
async def test_read_denies_escape_and_revalidates_dynamic_permission(uart):
    _, workspaces, context, _ = uart
    service = TaskActionService(workspaces=workspaces)
    stored = {}
    escape = context.model_copy(update={"allowed_rtl_paths": ("../outside.sv",)})
    result = await dispatch(service, escape, "read_rtl_source", {"relative_path": "../outside.sv"}, stored)
    assert result.outcome == "failed" and not stored
    selection = ToolSelection(name="read_rtl_source", arguments={"relative_path": "rtl/uart.sv"})
    action = service.authorize_task_action(context, selection, [tool_registry.get(selection.name).definition])
    assert isinstance(action, AuthorizedAction)
    tool_registry.set_enabled(selection.name, False)
    try:
        result = await service.execute_task_action(context, action, stage_output(context, stored))
        assert result.error_code == "tool_unavailable" and not stored
    finally:
        tool_registry.set_enabled(selection.name, True)


def test_approval_is_bound_to_exact_action_and_arguments(uart):
    _, workspaces, context, _ = uart
    registry = ToolRegistry()
    registry.register(
        name="lint", description="Controlled execution", allowed_roles=[AgentRole.DESIGN],
        mutability=Mutability.EXECUTION, scope="shared", capability="lint",
        requires_user_approval=True, idempotent=False, timeout_seconds=10,
    )
    service = TaskActionService(registry=registry, workspaces=workspaces)
    context = context.model_copy(update={
        "permitted_tools": ("lint",),
        "contract": TaskContract.model_validate({
            **task_node("lint", "design", ("lint",), ("lint_report",)),
            "constraints": {"read_only": False, "allowed_mutability": ["execution"]},
        }),
    })
    selection = ToolSelection(name="lint", arguments={})
    shortlist = [registry.get("lint").definition]
    result = service.authorize_task_action(context, selection, shortlist)
    assert result.outcome == "waiting_for_approval" and result.wait.kind == "tool_approval"
    approval = ActionApproval(
        run_id=context.run_id, task_id=context.contract.task_id, generation=context.generation,
        action_id=context.action_id, workspace_id=context.workspace_id, tool_name="lint",
        argument_hash=hashlib.sha256(b"{}").hexdigest(),
    )
    assert isinstance(service.authorize_task_action(context, selection, shortlist, approval), AuthorizedAction)
    changed = selection.model_copy(update={"arguments": {"top_module": "other"}})
    assert service.authorize_task_action(context, changed, shortlist, approval).outcome == "waiting_for_approval"


@pytest.mark.asyncio
async def test_submission_only_stages_a_candidate(uart):
    _, workspaces, context, _ = uart
    node = task_node("report", "design", ("compose_report",), ("answer",))
    node["outputs"][0]["media_type"] = "text/markdown"
    node["outputs"][0]["artifact_type"] = "report"
    context = context.model_copy(update={"contract": TaskContract.model_validate(node), "action_count": 8})
    stored = {}
    result = await dispatch(
        TaskActionService(workspaces=workspaces), context, "submit_task_output",
        {"output_keys": ["answer"], "text_outputs": {"answer": "UART review"}}, stored,
    )
    assert result.outcome == "observed"
    candidate = stored[result.candidate_artifact_ids[0]]
    assert candidate.content == b"UART review" and candidate.artifact.status == "staged"
