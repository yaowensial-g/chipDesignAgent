import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.runtime import runtime
from app.domain.enums import AgentRole
from app.main import app
from app.services.delegation_service import delegation_service
from app.services.tool_selector_service import tool_selector_service


client = TestClient(app)


def _chat(message: str, role: str, *, session_id: str | None = None) -> dict:
    body = {
        "project_id": f"role-runtime-{uuid4().hex[:8]}",
        "module_name": "scheduler",
        "message": message,
        "user_role": role,
        "documents": [
            {
                "kind": "AS",
                "name": "DMA AS",
                "version": "1.0",
                "content": "SUBMODULE: command_queue\nSUBMODULE: scheduler",
            }
        ],
    }
    if session_id:
        body["session_id"] = session_id
    return body


def test_user_role_persists_and_selects_primary_agent() -> None:
    response = client.post(
        "/api/v1/chat",
        json=_chat("解释 ready/valid 握手", "verification_engineer"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["primary_agent"] == "verification"

    conversation = client.get(f"/api/v1/conversations/{payload['session_id']}")
    assert conversation.status_code == 200
    assert conversation.json()["user_role"] == "verification_engineer"
    assert conversation.json()["primary_agent"] == "verification"


def test_tool_catalog_is_filtered_before_planning() -> None:
    verification = client.get("/api/v1/tools?role=verification")
    design = client.get("/api/v1/tools?role=design")
    assert verification.status_code == 200
    assert design.status_code == 200

    verification_names = {item["name"] for item in verification.json()}
    design_names = {item["name"] for item in design.json()}
    assert "consult_design_agent" in verification_names
    assert "create_ds_candidate" not in verification_names
    assert "create_rtl_candidate" not in verification_names
    assert "create_ds_candidate" in design_names
    assert "consult_verification_agent" in design_names


def test_selector_records_permission_safe_shortlist() -> None:
    response = client.post(
        "/api/v1/chat",
        json=_chat("这个系统能做什么？", "verification_engineer"),
    )
    assert response.status_code == 200
    run = response.json()["run"]
    traces = run["result"]["tool_selections"]
    assert traces
    assert all(trace["primary_agent"] == "verification" for trace in traces)
    assert all("create_ds_candidate" not in trace["selected_tools"] for trace in traces)
    assert all(len(trace["selected_tools"]) <= 6 for trace in traces)
    assert all(trace["metadata_count"] == trace["authorized_count"] for trace in traces)


def test_selector_rejects_tool_name_outside_role_permissions(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime,
        "select_tool_names",
        lambda *args, **kwargs: {
            "selected_tools": ["create_ds_candidate", "reply_to_user"],
            "reason": "malicious or mistaken selector output",
        },
    )
    shortlist, _ = tool_selector_service.select(
        primary_agent=AgentRole.VERIFICATION,
        request={"message": "test"},
        intent={"candidate_tools": []},
        observations=[],
    )
    assert "create_ds_candidate" not in {tool.name for tool in shortlist}
    assert "reply_to_user" in {tool.name for tool in shortlist}


def test_verification_primary_delegates_design_analysis() -> None:
    response = client.post(
        "/api/v1/chat",
        json=_chat("请让设计专家分析 RTL 实现中的根因", "verification_engineer"),
    )
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["primary_agent"] == "verification"
    assert run["result"]["delegations"], run["result"]
    delegation = run["result"]["delegations"][0]
    assert delegation["from_agent"] == "verification"
    assert delegation["to_agent"] == "design"
    assert delegation["status"] == "completed"

    persisted = client.get(f"/api/v1/runs/{run['run_id']}/delegations")
    assert persisted.status_code == 200
    assert persisted.json()[0]["task_id"] == delegation["task_id"]


def test_role_switch_keeps_session_but_changes_primary_agent() -> None:
    project_id = f"role-switch-{uuid4().hex[:8]}"
    first = _chat("解释 scheduler 的职责", "design_engineer")
    first["project_id"] = project_id
    created = client.post("/api/v1/chat", json=first).json()

    second = _chat(
        "从验证角度继续分析",
        "verification_engineer",
        session_id=created["session_id"],
    )
    second["project_id"] = project_id
    switched = client.post("/api/v1/chat", json=second)
    assert switched.status_code == 200
    assert switched.json()["session_id"] == created["session_id"]
    assert switched.json()["run"]["primary_agent"] == "verification"

    events = client.get(
        f"/api/v1/runs/{switched.json()['run']['run_id']}/events"
    ).json()
    assert any(event["event_type"] == "conversation_role_changed" for event in events)


def test_session_cannot_be_reused_across_projects() -> None:
    created = client.post(
        "/api/v1/chat",
        json=_chat("解释 scheduler", "design_engineer"),
    ).json()
    conflicting = _chat(
        "继续",
        "design_engineer",
        session_id=created["session_id"],
    )
    conflicting["project_id"] = "another-project"
    response = client.post("/api/v1/chat", json=conflicting)
    assert response.status_code == 409
    assert response.json()["detail"] == "Conversation belongs to a different project"


def test_architecture_expert_cannot_start_nested_delegation() -> None:
    with pytest.raises(PermissionError):
        asyncio.run(
            delegation_service.delegate(
                parent_run_id="not-used-because-policy-fails-first",
                from_agent=AgentRole.ARCHITECTURE,
                to_agent=AgentRole.DESIGN,
                task_type="nested",
                instruction="try nested delegation",
                request_payload={"project_id": "p", "module_name": "m"},
            )
        )
