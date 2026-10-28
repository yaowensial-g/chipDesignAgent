import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.skills import skill_catalog


client = TestClient(app)


def test_role_skills_are_versioned_and_loadable() -> None:
    assert "受控原始资料" in skill_catalog.load("architecture/specification-analysis")
    assert "Markdown Candidate" in skill_catalog.load("design/ds-authoring")
    assert "Diff" in skill_catalog.load("design/rtl-implementation")
    assert "独立于生成者" in skill_catalog.load("review/independent-review")


def _documents() -> list[dict]:
    return [
        {"kind": "FS", "name": "DMA FS", "version": "1.0", "content": "支持 DMA 命令处理。"},
        {
            "kind": "AS",
            "name": "DMA AS",
            "version": "1.0",
            "content": "SUBMODULE: command_queue\nSUBMODULE: scheduler",
        },
    ]


def _chat(message: str, **updates) -> dict:
    body = {
        "project_id": f"run-test-{uuid4().hex[:8]}",
        "module_name": "scheduler",
        "message": message,
        "documents": _documents(),
    }
    body.update(updates)
    return body


def test_chat_created_ds_is_linked_to_unified_run() -> None:
    response = client.post("/api/v1/chat", json=_chat("请完成 scheduler 的 DS"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "ds_workflow"
    assert payload["run"]["run_type"] == "ds_design"
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["legacy_task_id"] == payload["task"]["task_id"]

    fetched = client.get(f"/api/v1/runs/{payload['run']['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["result"]["task"]["ds_markdown"] == payload["task"]["ds_markdown"]
    assert client.get(f"/api/v1/runs/{payload['run']['run_id']}/events").json()


def test_conversation_endpoint_uses_path_session_id() -> None:
    session_id = f"session-{uuid4().hex[:8]}"
    response = client.post(
        f"/api/v1/conversations/{session_id}/messages",
        json=_chat("解释一下 ready/valid 握手"),
    )
    assert response.status_code == 200
    assert response.json()["session_id"] == session_id
    assert response.json()["run"] is None


def test_rtl_chat_creates_reviewable_patch_run() -> None:
    body = _chat(
        "根据已经确认的 DS 生成 RTL",
        ds_markdown="# scheduler DS\n已确认。",
    )
    response = client.post("/api/v1/chat", json=body)
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["run_type"] == "rtl_implementation"
    assert run["status"] == "waiting_for_approval"
    assert run["patch_id"]
    workspace_root = Path(run["result"]["workspace"]["root_path"])
    assert not (workspace_root / "rtl" / "scheduler.sv").exists()

    approved = client.post(
        f"/api/v1/runs/{run['run_id']}/resume",
        json={"action": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert (workspace_root / "rtl" / "scheduler.sv").exists()


def test_lint_chat_starts_tool_run_and_refreshes_unified_status() -> None:
    project_id = f"lint-run-{uuid4().hex[:8]}"
    workspace = client.post("/api/v1/workspaces", json={"project_id": project_id}).json()
    response = client.post(
        "/api/v1/chat",
        json=_chat(
            "执行 scheduler 的 lint",
            project_id=project_id,
            workspace_id=workspace["workspace_id"],
            tool_arguments={"files": ["rtl/missing.sv"]},
        ),
    )
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["run_type"] == "lint"
    assert run["tool_run_id"]

    current = run
    for _ in range(50):
        current = client.get(f"/api/v1/runs/{run['run_id']}").json()
        if current["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert current["status"] == "failed"
    assert current["result"]["tool_run"]["status"] == "failed"


def test_unregistered_engineering_stage_is_honest_about_missing_adapter() -> None:
    response = client.post("/api/v1/chat", json=_chat("执行 scheduler 的综合"))
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["run_type"] == "synthesis"
    assert run["status"] == "waiting_for_user"
    assert run["pending_user_request"]["type"] == "tool_adapter_required"


def test_cancelling_waiting_ds_run_also_cancels_linked_task() -> None:
    body = _chat("请完成 scheduler 的 DS")
    body["documents"][0]["content"] += " [CONFLICT]"
    created = client.post("/api/v1/chat", json=body).json()
    assert created["run"]["status"] == "waiting_for_user"

    cancelled = client.post(f"/api/v1/runs/{created['run']['run_id']}/cancel")
    assert cancelled.status_code == 200
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["result"]["task"]["status"] == "cancelled"

