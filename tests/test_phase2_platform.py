import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.models import CreateDSTaskRequest, UserResolution
from app.main import app
from app.services.task_service import TaskService
from app.services.tool_run_service import parse_tool_issues
from app.storage.database import Database


client = TestClient(app)


def _documents(conflict: bool = False) -> list[dict]:
    return [
        {"kind": "FS", "name": "FS", "version": "1.0", "content": "DMA 功能。"},
        {
            "kind": "AS",
            "name": "AS",
            "version": "1.0",
            "content": f"SUBMODULE: scheduler\n{'[CONFLICT]' if conflict else ''}",
        },
    ]


def test_workspace_patch_requires_review_before_write() -> None:
    workspace = client.post(
        "/api/v1/workspaces", json={"project_id": "phase2-patch-test"}
    ).json()
    target = Path(workspace["root_path"]) / "rtl" / "demo.sv"
    patch_response = client.post(
        "/api/v1/patches",
        json={
            "workspace_id": workspace["workspace_id"],
            "target_path": "rtl/demo.sv",
            "after_content": "module demo; endmodule\n",
            "reason": "test candidate",
            "sources": ["DS-v1"],
        },
    )
    assert patch_response.status_code == 201
    patch = patch_response.json()
    assert patch["status"] == "patch_ready"
    assert not target.exists()

    approved = client.post(
        f"/api/v1/patches/{patch['patch_id']}/decision",
        json={"action": "approve", "comment": "reviewed"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "module demo; endmodule\n"


def test_workspace_rejects_path_escape() -> None:
    workspace = client.post(
        "/api/v1/workspaces", json={"project_id": "phase2-path-test"}
    ).json()
    response = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/artifacts",
        json={
            "artifact_type": "rtl",
            "relative_path": "../outside.sv",
            "content": "unsafe",
        },
    )
    assert response.status_code == 422


def test_rtl_candidate_is_a_patch_not_a_direct_write() -> None:
    response = client.post(
        "/api/v1/rtl/candidates",
        json={
            "project_id": "phase2-rtl-test",
            "module_name": "scheduler",
            "target_path": "rtl/scheduler.sv",
            "ds_markdown": "# scheduler DS\n已确认的设计。",
            "documents": _documents(),
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["patch"]["status"] == "patch_ready"
    assert "module scheduler" in payload["candidate_content"]
    assert not (Path(payload["workspace"]["root_path"]) / "rtl" / "scheduler.sv").exists()


def test_tool_requires_approval_and_reports_environment_issue() -> None:
    workspace = client.post(
        "/api/v1/workspaces", json={"project_id": "phase2-tool-test"}
    ).json()
    body = {
        "workspace_id": workspace["workspace_id"],
        "tool_name": "verilator_lint",
        "role": "design",
        "arguments": {"files": ["rtl/missing.sv"]},
        "user_approved": False,
    }
    assert client.post("/api/v1/tool-runs", json=body).status_code == 403
    body["user_approved"] = True
    started = client.post("/api/v1/tool-runs", json=body)
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    run = started.json()
    for _ in range(50):
        run = client.get(f"/api/v1/tool-runs/{run_id}").json()
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert run["status"] == "failed"
    issue = client.get(f"/api/v1/tool-runs/{run_id}/issues/next").json()
    assert issue["status"] == "awaiting_user"
    assert issue["code"] in {"TOOL_ENVIRONMENT", "TOOL_ARGUMENT"}


def test_verilator_log_parser_deduplicates_findings() -> None:
    text = "%Error-WIDTH: rtl/a.sv:12:5: width mismatch\n%Error-WIDTH: rtl/a.sv:12:5: width mismatch"
    issues = parse_tool_issues("verilator_lint", text)
    assert len(issues) == 1
    assert issues[0]["file"] == "rtl/a.sv"
    assert issues[0]["line"] == 12


@pytest.mark.asyncio
async def test_waiting_task_can_resume_from_sqlite_snapshot(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    first_service = TaskService(db)
    request = CreateDSTaskRequest(
        project_id="persist-test",
        module_name="scheduler",
        documents=_documents(conflict=True),
        max_discussion_rounds=1,
    )
    waiting = await first_service.create_ds_task(request)
    assert waiting.status.value == "waiting_for_user"

    restarted_service = TaskService(db)
    loaded = restarted_service.get_task(waiting.task_id)
    assert loaded is not None
    assert loaded.pending_user_request is not None
    delivered = await restarted_service.resume_task(
        waiting.task_id,
        UserResolution(
            action="provide_resolution",
            content="真实架构师确认采用该握手定义。",
            evidence_reference="ISSUE-PERSIST-1",
        ),
    )
    assert delivered.status.value == "delivered"
    assert "ISSUE-PERSIST-1" in (delivered.ds_markdown or "")
