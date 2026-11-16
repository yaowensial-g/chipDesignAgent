import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app.domain.models import ChatRequest, PatchCreateRequest, PatchDecisionRequest, RunResumeRequest, ToolSelection, WorkspaceCreateRequest
from app.domain.orchestration import ReflectionResult
from app.services.config_completion_service import ConfigCompletionService
from app.services.config_extractor_service import ConfigExtractorService
from app.services.config_service import ConfigService
from app.tools.adapters.base import EDAToolResult
from app.tools.eda_inspection import module_hierarchy, signal_references, signal_trace
from app.tools.project_manifest import read_project

from test_hierarchical_run import ScriptedRuntime, executor_factory
from test_verilator_stage import fake_verilator


FIXTURE = Path(__file__).parent / "fixtures" / "uart_project"


class ProjectRuntime(ScriptedRuntime):
    def reflect_and_select(self, contract, context, shortlist, evidence):
        capability = contract.capability_requirements[0]
        if capability == "compose_report" or any(item["status"] == "staged" for item in context["artifacts"]):
            name = "submit_task_output"
            arguments = {"output_keys": ["result"]}
            if capability == "compose_report":
                arguments["text_outputs"] = {"result": "UART evidence\n" + "\n".join(
                    f"[artifact:{item['artifact_id']}]" for item in context["artifacts"]
                )}
        elif context["observations"]:
            name = "report_task_failure"
            arguments = {"reason": context["observations"][-1]["summary"]}
        else:
            name = capability
            arguments = dict(context["eda_arguments"][name])
            if name == "propose_eda_patch":
                arguments["after_content"] = context["repair_source"].replace("tx <= 1'(`UART_RESET);", "tx <= 1'b0;")
        assert name in {item.name for item in shortlist}
        decision = {"submit_task_output": "submit", "report_task_failure": "fail"}.get(name, "continue")
        return ReflectionResult(
            progress="in_progress", progress_summary="Project operation", satisfied_criteria=(),
            unmet_criteria=("evidence",), issues=(), decision=decision,
            reason="Use only requested project arguments", evidence=evidence,
        ), ToolSelection(name=name, arguments=arguments)


def project_request(executor, operations, **kwargs):
    workspace = executor.actions.workspaces.create(WorkspaceCreateRequest(project_id="uart-eda"))
    root = executor.actions.workspaces.resolve_path(workspace.workspace_id, ".")
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    return ChatRequest(
        project_id="uart-eda", module_name="uart_top", message="Check this UART project",
        workspace_id=workspace.workspace_id, orchestration_mode="eda_project", eda_operations=operations, **kwargs,
    ), root


async def drive_approved(executor, run, approvals=None):
    for _ in range(3000):
        if run.status.value in {"failed", "completed"}:
            return run
        if run.status.value == "waiting_for_approval":
            pending = run.pending_user_request
            if approvals is not None:
                approvals.append(pending["eda_target"])
            run = await executor.resume(run.run_id, RunResumeRequest(
                action="approve", wait_id=pending["wait_id"], client_request_id=pending["wait_id"],
            ))
        else:
            await executor.poll_external()
            await asyncio.sleep(0.04)
            run = executor.runs.get(run.run_id)
    pytest.fail("EDA Run did not finish")


@pytest.mark.asyncio
async def test_project_inspection_uses_manifest_and_existing_task_chain(executor_factory):
    executor = executor_factory(ProjectRuntime())
    request, _ = project_request(executor, ["get_module_hierarchy", "find_signal_references"], eda_signal="tx")
    result = await executor.handle(request)
    assert result.run.status.value == "completed", result.run.result
    outputs = {
        item.artifact.artifact_type: json.loads(item.content)
        for state in executor.repo.states(result.run.run_id).values()
        for item in executor.repo.local_outputs(state.task_id, "verified")
        if item.artifact.artifact_type != "report"
    }
    manifest = outputs["project_manifest"]
    assert manifest["manifest"]["defines"] == ["UART_RESET=1"]
    assert outputs["get_module_hierarchy"]["modules"]["uart_top"]["instances"][0]["module"] == "uart_tx"
    assert {"declaration", "assignment", "reference"} <= {
        item["kind"] for item in outputs["find_signal_references"]["references"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "tested_value", "edited_after_freeze", "expected"), [
    ("run_testcase", 64, False, "verified"),
    ("run_testcase", 32, False, "implemented"),
    ("run_lint", 64, False, "implemented"),
    ("run_testcase", 64, True, "pending"),
])
async def test_completion_requires_matching_current_rtl_and_targeted_simulation(
    executor_factory, fake_verilator, monkeypatch, tool, tested_value, edited_after_freeze, expected,
):
    from app.services import task_action_service
    from app.tools import eda_catalog
    from app.tools.adapters import verilator_adapter

    for module in (task_action_service, eda_catalog, verilator_adapter):
        monkeypatch.setattr(module, "simulation_environment", lambda _: {"test": True})

    def execute(name, executable, version, project, cases, seed, root, *, coverage=False):
        return EDAToolResult(
            tool_name=name, tool_version=version, input_hash=project.input_hash, status="PASS", exit_code=0,
            commands=[[executable, "--cc" if cases else "--lint-only"]],
            metrics={"testcases": [{"testcase": case, "status": "PASS", "exit_code": 0} for case in cases]},
        ), "", "", {}

    monkeypatch.setattr(verilator_adapter, "execute", execute)
    executor = executor_factory(ProjectRuntime())
    request, root = project_request(executor, [tool], eda_testcases=["uart_pass"] if tool != "run_lint" else [])
    rtl = root / "rtl/uart_top.sv"
    rtl.write_text(rtl.read_text(encoding="utf-8").replace(
        "module uart_top (", "module uart_top #(parameter int FIFO_DEPTH = 64) (",
    ), encoding="utf-8")
    source = root / "chipdesign.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["testcases"]["uart_pass"].update(
        depends_on=["rtl/uart_top.sv"],
        config_checks=[{"module": "uart_top", "parameter": "fifo_depth", "value": tested_value}],
    )
    source.write_text(json.dumps(manifest), encoding="utf-8")

    configs = ConfigService(executor.runs.db, executor.actions.workspaces)
    executor.config_extractor = ConfigExtractorService(configs, executor.repo.sessions,
        lambda prompt, payload: {"updates": [{
            "module": "uart_top", "parameter": "fifo_depth", "value": 64, "evidence": payload["current_request"],
        }]},
    )
    completions = []
    checker = ConfigCompletionService(configs)

    def completed(run):
        completions.append(run.run_id)
        checker.complete(run.run_id)

    executor.on_completed = completed
    initial = (await executor.handle(request.model_copy(update={"message": "Set FIFO depth to 64 and check UART"}))).run
    assert initial.status.value == "waiting_for_approval", initial.result
    assert configs.get_pending_items("uart-eda", "uart_top")
    assert not completions
    if edited_after_freeze:
        rtl.write_text(rtl.read_text(encoding="utf-8").replace("FIFO_DEPTH = 64", "FIFO_DEPTH = 128"), encoding="utf-8")
    final = await drive_approved(executor, initial)
    assert final.status.value == "completed", final.result
    assert completions == [final.run_id]
    item = configs.get_config("uart-eda", "uart_top").modules["uart_top"].parameters["fifo_depth"]
    assert item.status == expected
    if expected == "verified":
        assert [reference.kind for reference in item.evidence_refs] == ["artifact", "tool_run"]
        assert item.evidence_refs[-1].testcase == "uart_pass"


def test_manifest_rejects_external_filelist_arguments_and_includes(executor_factory):
    executor = executor_factory(ProjectRuntime())
    request, root = project_request(executor, [])
    args = (executor.actions.workspaces, request.workspace_id, request.project_id, "chipdesign.json")
    project = read_project(*args)
    assert module_hierarchy(project)["top_module"] == "uart_top"
    assert signal_references(project, "tx")["references"]
    filelist = root / "rtl.f"
    baseline = filelist.read_bytes()
    filelist.write_bytes(baseline + b"\n--exe unapproved.cpp\n")
    with pytest.raises(ValueError):
        read_project(*args)
    filelist.write_bytes(baseline)
    path = root / "rtl/uart_tx.sv"
    path.write_bytes(b'`include "../outside.svh"\n' + path.read_bytes())
    with pytest.raises(ValueError, match="Workspace-relative"):
        read_project(*args)


def test_vcd_trace_uses_rising_edges_and_keeps_unknown_values():
    vcd = b"""$scope module top $end
$var wire 1 ! clk $end
$var wire 1 # valid $end
$var wire 1 & ready $end
$upscope $end
$enddefinitions $end
#0
0!
1#
1&
#1
1!
#2
0!
x#
#3
1!
"""
    result = signal_trace(vcd, "valid", "top.clk", "ready")
    assert (result["clock_edges"], result["handshakes"], result["unknown_cycles"]) == (2, 1, 1)
    assert result["changes"][-1] == {"tick": 2, "value": "x"}


@pytest.mark.asyncio
async def test_new_run_links_only_an_applied_patch_to_frozen_source(executor_factory):
    executor = executor_factory(ProjectRuntime())
    request, root = project_request(executor, [])
    path = "rtl/uart_tx.sv"
    modified = (root / path).read_text(encoding="utf-8") + "\n// reviewed change\n"
    patch = executor.runs.patches.create(PatchCreateRequest(
        workspace_id=request.workspace_id, target_path=path, after_content=modified, reason="Review UART change",
    ))
    scoped = request.model_copy(update={"eda_applied_patch_id": patch.patch_id})
    blocked = (await executor.handle(scoped)).run
    assert blocked.status.value == "failed"
    executor.runs.patches.decide(patch.patch_id, PatchDecisionRequest(action="approve"))
    completed = await executor.handle(scoped)
    assert completed.run.status.value == "completed", completed.run.result
    assert patch.patch_id in completed.assistant_message
    (root / path).write_text(modified + "\n// different later edit\n", encoding="utf-8")
    stale = (await executor.handle(scoped)).run
    assert stale.status.value == "failed"


@pytest.mark.asyncio
async def test_real_multifile_compile_lint_and_approved_snapshot(executor_factory, monkeypatch):
    executable = os.getenv("CHIP_AGENT_TEST_REAL_VERILATOR")
    if not executable:
        pytest.skip("set CHIP_AGENT_TEST_REAL_VERILATOR")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_ENABLED", "1")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_PATH", executable)
    executor = executor_factory(ProjectRuntime())
    request, root = project_request(executor, ["run_compile", "run_lint", "parse_eda_log"])
    run = (await executor.handle(request)).run
    assert run.status.value == "waiting_for_approval", run.result
    (root / "rtl/uart_top.sv").write_text("unrelated invalid later edit", encoding="utf-8")
    targets = []
    finished = await drive_approved(executor, run, targets)
    assert finished.status.value == "completed", finished.result
    assert len(targets) == 2 and targets[0]["content_hash"] == targets[1]["content_hash"]
    assert [(item["tool_name"], item["status"]) for item in finished.result["eda_tool_runs"]] == [
        ("run_compile", "PASS"), ("run_lint", "PASS"),
    ]
    for item in finished.result["eda_tool_runs"]:
        assert executor.runs.tools.eda_result(item["tool_run_id"]).tool_version.startswith("Verilator")


@pytest.mark.asyncio
async def test_real_uart_suite_reports_failed_case_and_saves_waveform(executor_factory, monkeypatch):
    executable = os.getenv("CHIP_AGENT_TEST_REAL_VERILATOR")
    if not executable or not shutil.which("g++"):
        pytest.skip("requires native Verilator and g++")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_ENABLED", "1")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_PATH", executable)
    executor = executor_factory(ProjectRuntime())
    request, root = project_request(
        executor, ["run_test_suite", "parse_eda_log", "get_waveform_artifact", "analyze_signal_trace", "propose_eda_patch"],
        eda_testcases=["uart_pass", "uart_fail"], eda_seed=42,
        eda_wave_testcase="uart_fail", eda_signal="uart_fail.tx", eda_clock="uart_fail.clk",
        eda_repair_path="rtl/uart_tx.sv",
    )
    run = (await executor.handle(request)).run
    assert run.status.value == "waiting_for_approval", run.result
    finished = await drive_approved(executor, run)
    assert finished.status.value == "failed" and "final_artifact_id" in finished.result, finished.result
    tool_run_id = finished.result["eda_tool_runs"][0]["tool_run_id"]
    result = executor.runs.tools.eda_result(tool_run_id)
    assert isinstance(result, EDAToolResult)
    tool = executor.runs.tools.get(tool_run_id)
    assert [(item["testcase"], item["status"], item["seed"]) for item in result.metrics["testcases"]] == [
        ("uart_pass", "PASS", 42), ("uart_fail", "FAIL", 42),
    ], tool.stderr
    assert any("intentional UART expectation failure" in item["message"] for item in result.issues)
    wave = executor.runs.tools.eda_artifact(tool_run_id, "uart_fail.vcd")
    assert hashlib.sha256(wave).hexdigest() == next(
        item["sha256"] for item in result.artifacts if item["name"] == "uart_fail.vcd"
    )
    assert "$enddefinitions" in wave.decode()
    outputs = {
        item.artifact.artifact_type: json.loads(item.content)
        for state in executor.repo.states(finished.run_id).values()
        for item in executor.repo.local_outputs(state.task_id, "verified")
        if item.artifact.artifact_type in {"get_waveform_artifact", "analyze_signal_trace", "propose_eda_patch"}
    }
    assert outputs["get_waveform_artifact"]["available"]
    assert outputs["analyze_signal_trace"]["trace"]["changes"]
    patch = outputs["propose_eda_patch"]
    assert patch["status"] == "patch_ready" and "tx <= 1'b0;" in patch["diff"]
    assert (root / "rtl/uart_tx.sv").read_bytes() == (FIXTURE / "rtl/uart_tx.sv").read_bytes()
    executor.runs.patches.decide(patch["patch_id"], PatchDecisionRequest(action="approve"))
    retest = request.model_copy(update={
        "eda_operations": ["run_testcase", "get_coverage_summary"],
        "eda_testcases": ["uart_fail"], "eda_applied_patch_id": patch["patch_id"],
    })
    rerun = await drive_approved(executor, (await executor.handle(retest)).run)
    assert rerun.status.value == "completed", rerun.result
    post = executor.runs.tools.eda_result(rerun.result["eda_tool_runs"][0]["tool_run_id"])
    assert post.status == "PASS" and post.input_hash != result.input_hash
    [coverage] = [
        json.loads(item.content) for state in executor.repo.states(rerun.run_id).values()
        for item in executor.repo.local_outputs(state.task_id, "verified")
        if item.artifact.artifact_type == "get_coverage_summary"
    ]
    assert coverage["testcases"]["uart_fail"]["points"] > 0 and not coverage["missing_testcases"]
