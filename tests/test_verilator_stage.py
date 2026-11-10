import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.models import RunResumeRequest, ToolSelection
from app.domain.enums import ToolRunStatus
from app.domain.orchestration import LintResult, ReflectionResult
from app.services.global_planner_service import GlobalPlannerService
from app.main import app
from app.services.orchestrator_service import orchestrator_service
from app.services.workspace_service import workspace_service
from app.tools import builtin

from test_hierarchical_run import ScriptedRuntime, SimulatedRestart, UART, request_for, rtl_plan, executor_factory
from test_task_contracts import task_node, upstream


def lint_plan(payload):
    assert "lint" in payload["context"]["workflow_contract"]
    draft = rtl_plan(payload)
    report = draft["tasks"][0]
    lint = task_node(
        "lint", "design", ("real_rtl_lint",), ("lint_result",), ("design",),
        (upstream("design", "rtl_snapshot"),),
    )
    lint["constraints"] = {"read_only": False, "allowed_mutability": ["analysis", "execution"]}
    report["depends_on"] = (*report["depends_on"], "lint")
    report["inputs"] = (*report["inputs"], upstream("lint", "lint_result"))
    draft["tasks"].insert(1, lint)
    return draft


class LintRuntime(ScriptedRuntime):
    def reflect_and_select(self, contract, context, shortlist, evidence):
        if "real_rtl_lint" not in contract.capability_requirements:
            return super().reflect_and_select(contract, context, shortlist, evidence)
        names = {tool.name for tool in shortlist}
        if any(item["artifact_type"] == "lint_result" for item in context["artifacts"]):
            name, decision, arguments = "submit_task_output", "submit", {"output_keys": ["lint_result"]}
        elif "verilator_lint" in names and not context["observations"]:
            name, decision, arguments = "verilator_lint", "continue", context["verilator_lint_arguments"]
        else:
            name, decision, arguments = "report_task_failure", "fail", {"reason": "Lint unavailable"}
        return ReflectionResult(
            progress="in_progress", progress_summary="Prepare or submit Lint evidence",
            satisfied_criteria=(), unmet_criteria=("deliver",), issues=(),
            decision=decision, reason="Use the approved frozen input", evidence=evidence,
        ), ToolSelection(name=name, arguments=arguments)

    @staticmethod
    def _finding(payload):
        snapshot = payload["snapshot"]
        return {
            "statement": "The UART fixture contains a reset block.",
            "evidence": [{
                "snapshot_ref": snapshot["content_ref"], "content_hash": snapshot["content_hash"],
                "start_line": 12, "end_line": 18,
            }],
        }


def lint_executor(executor_factory):
    executor = executor_factory(LintRuntime())
    executor.planner = GlobalPlannerService(propose=lint_plan)
    return executor


@pytest.fixture
def fake_verilator(tmp_path, monkeypatch):
    from app.services import tool_run_service

    executable = tmp_path / "verilator.exe"
    executable.write_bytes(b"test-only executable identity")
    executable.chmod(0o755)
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_ENABLED", "1")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_PATH", str(executable))
    monkeypatch.setattr(builtin, "_probe_verilator", lambda *args: "Verilator test double")
    calls = []

    def run_lint(executable, files, cwd, top_module):
        calls.append(((cwd / files[0]).read_bytes(), top_module))
        return [executable, "--lint-only", *files], 0, "Test-only log", ""

    monkeypatch.setattr(tool_run_service, "run_verilator_lint", run_lint)
    return executable, calls


async def wait_for_terminal(executor, run_id):
    for _ in range(300):
        await executor.poll_external()
        run = executor.runs.get(run_id)
        if run.status.value in {"completed", "failed"}:
            return run
        await asyncio.sleep(0.02)
    pytest.fail("ToolRun did not reach a terminal state")


async def start_lint(executor, *, malformed=False):
    request = request_for(executor).model_copy(update={"run_lint": True})
    path = executor.actions.workspaces.resolve_path(request.workspace_id, request.rtl_path)
    if malformed:
        path.write_bytes(UART.read_bytes().replace(b"endmodule", b"endmodul"))
    run = (await executor.handle(request)).run
    assert run.status.value == "waiting_for_approval", run.result
    return run, path


def approve(run):
    return RunResumeRequest(action="approve", wait_id=run.pending_user_request["wait_id"], client_request_id="approved")


@pytest.mark.asyncio
async def test_approval_binds_snapshot_and_duplicate_resume_does_not_execute_twice(executor_factory, fake_verilator):
    executor = lint_executor(executor_factory)
    run, path = await start_lint(executor)
    _, calls = fake_verilator
    assert not calls and executor.repo.latest_tool_run_id(run.run_id) is None
    pending = run.pending_user_request
    assert pending["lint_target"]["top_module"] == "uart" and pending["approval"]["input_hash"]
    path.write_bytes(b"module replacement; endmodule")
    decision = approve(run)
    with pytest.raises(ValueError, match="Unsupported resume arguments"):
        await executor.resume(run.run_id, decision.model_copy(update={"tool_arguments": {"files": ["other.sv"]}}))
    await executor.resume(run.run_id, decision)
    await executor.resume(run.run_id, decision)
    completed = await wait_for_terminal(executor, run.run_id)
    assert completed.status.value == "completed"
    assert calls == [(UART.read_bytes(), "uart")]
    assert completed.result["lint_tool_run_id"]
    lint_task = executor.repo.states(run.run_id)[pending["task_id"]]
    assert lint_task.action_count == 1 and lint_task.current_iteration == 2
    assert executor.repo.verifications(lint_task.task_id)[0]["result"]["passed"]
    [artifact] = executor.repo.local_outputs(lint_task.task_id, "verified")
    proof = LintResult.model_validate_json(artifact.content)
    assert proof.content_hash == pending["lint_target"]["content_hash"]
    assert proof.tool_run_id == completed.result["lint_tool_run_id"]


@pytest.mark.asyncio
async def test_rejection_and_replaced_executable_never_dispatch(executor_factory, fake_verilator):
    executable, calls = fake_verilator
    executor = lint_executor(executor_factory)
    run, _ = await start_lint(executor)
    rejected = await executor.resume(run.run_id, approve(run).model_copy(update={"action": "reject"}))
    assert rejected.status.value == "failed" and "拒绝" in rejected.result["message"]
    assert not calls and executor.repo.latest_tool_run_id(run.run_id) is None

    another, _ = await start_lint(executor)
    executable.write_bytes(b"changed executable identity")
    result = await executor.resume(another.run_id, approve(another))
    assert result.status.value == "failed" and "发生变化" in result.result["message"]
    assert not calls and executor.repo.latest_tool_run_id(another.run_id) is None


@pytest.mark.asyncio
async def test_queued_tool_run_is_recovered_by_same_action(executor_factory, fake_verilator, monkeypatch):
    executor = lint_executor(executor_factory)
    run, _ = await start_lint(executor)
    task_id = run.pending_user_request["task_id"]

    def crash(*args):
        raise SimulatedRestart

    monkeypatch.setattr(executor.runs.tools.executor, "submit", crash)
    with pytest.raises(SimulatedRestart):
        await executor.resume(run.run_id, approve(run))
    tool_run_id = executor.repo.latest_tool_run_id(run.run_id)
    assert executor.runs.tools.get(tool_run_id).status.value == "queued"
    restarted = lint_executor(executor_factory)
    await restarted.recover()
    completed = await wait_for_terminal(restarted, run.run_id)
    assert completed.status.value == "completed" and completed.result["lint_tool_run_id"] == tool_run_id
    assert restarted.repo.states(run.run_id)[task_id].action_count == 1
    assert len(fake_verilator[1]) == 1


@pytest.mark.asyncio
async def test_running_tool_run_is_marked_unknown_and_never_relaunched(executor_factory, fake_verilator, monkeypatch):
    executor = lint_executor(executor_factory)
    run, _ = await start_lint(executor)

    def crash(*args):
        raise SimulatedRestart

    monkeypatch.setattr(executor.runs.tools.executor, "submit", crash)
    with pytest.raises(SimulatedRestart):
        await executor.resume(run.run_id, approve(run))
    tool_run_id = executor.repo.latest_tool_run_id(run.run_id)
    executor.runs.tools._set_status(tool_run_id, ToolRunStatus.RUNNING)
    restarted = lint_executor(executor_factory)
    await restarted.recover()
    failed = restarted.runs.get(run.run_id)
    assert failed.status.value == "failed"
    assert "TOOL_INTERRUPTED" in restarted.runs.tools.get(tool_run_id).stderr
    assert not fake_verilator[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_real_verilator_uart_integration(executor_factory, monkeypatch, malformed):
    executable = os.getenv("CHIP_AGENT_TEST_REAL_VERILATOR")
    if not executable:
        pytest.skip("set CHIP_AGENT_TEST_REAL_VERILATOR to run real EDA integration")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_ENABLED", "1")
    monkeypatch.setenv("CHIP_AGENT_VERILATOR_PATH", executable)
    assert builtin.verilator_available()
    executor = lint_executor(executor_factory)
    run, _ = await start_lint(executor, malformed=malformed)
    await executor.resume(run.run_id, approve(run))
    finished = await wait_for_terminal(executor, run.run_id)
    tool_run = executor.runs.tools.get(finished.result["lint_tool_run_id"])
    assert "--lint-only" in tool_run.command and Path(tool_run.command[0]).resolve() == Path(executable).resolve()
    if malformed:
        assert finished.status.value == "failed" and tool_run.exit_code != 0
        assert "%Error" in tool_run.stderr and executor.runs.tools.issues(tool_run.run_id)
        assert "final_artifact_id" not in finished.result
    else:
        assert finished.status.value == "completed" and tool_run.exit_code == 0
        assert finished.result["final_artifact_id"]


def test_timeout_output_is_decoded_before_persisting(monkeypatch, tmp_path):
    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=300, output=b"partial", stderr=b"timeout")

    monkeypatch.setattr(builtin.subprocess, "run", time_out)
    _, code, stdout, stderr = builtin.run_verilator_lint("verilator", ["uart.sv"], tmp_path, "uart")
    assert code == 124 and stdout == "partial" and stderr.startswith("timeout")


def test_public_approval_and_background_polling(monkeypatch, fake_verilator):
    monkeypatch.setenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "1")
    executor = orchestrator_service.hierarchical
    agent = LintRuntime()
    monkeypatch.setattr(executor, "runtime", agent)
    monkeypatch.setattr(executor.actions, "runtime", agent)
    monkeypatch.setattr(executor, "planner", GlobalPlannerService(propose=lint_plan))
    with TestClient(app) as client:
        workspace = client.post("/api/v1/workspaces", json={"project_id": "verilator-stage-c-api"}).json()
        workspace_id = workspace["workspace_id"]
        workspace_service.resolve_path(workspace_id, "rtl/uart.sv").write_bytes(UART.read_bytes())
        response = client.post("/api/v1/chat", json={
            "project_id": "verilator-stage-c-api", "module_name": "uart", "message": "Review UART RTL",
            "orchestration_mode": "rtl_analysis", "workspace_id": workspace_id,
            "rtl_path": "rtl/uart.sv", "run_lint": True,
        })
        assert response.status_code == 200, response.text
        run = response.json()["run"]
        assert run["status"] == "waiting_for_approval" and not fake_verilator[1]
        path = f"/api/v1/runs/{run['run_id']}"
        response = client.post(f"{path}/resume", json={
            "action": "approve", "wait_id": run["pending_user_request"]["wait_id"], "client_request_id": "lint-approval",
        })
        assert response.status_code == 200, response.text
        for _ in range(150):
            run = client.get(path).json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert run["status"] == "completed", run["result"]
        tool = client.get(f"/api/v1/tool-runs/{run['result']['lint_tool_run_id']}").json()
        assert tool["status"] == "completed" and tool["exit_code"] == 0
