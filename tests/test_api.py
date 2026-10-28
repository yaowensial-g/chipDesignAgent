from fastapi.testclient import TestClient

from app.config.model_settings import model_settings_service
from app.config.secret_protection import SecretProtectionError
from app.main import app


client = TestClient(app)


def _request(
    *,
    conflict: bool = False,
    disabled_agents: list[str] | None = None,
    custom_reviewers: list[str] | None = None,
    max_discussion_rounds: int = 2,
) -> dict:
    marker = "[CONFLICT]" if conflict else ""
    return {
        "project_id": "demo-project",
        "module_name": "dma_ctrl",
        "documents": [
            {
                "kind": "FS",
                "name": "DMA FS",
                "version": "1.0",
                "content": f"支持 DMA 命令处理。{marker}",
            },
            {
                "kind": "AS",
                "name": "DMA AS",
                "version": "1.0",
                "content": "SUBMODULE: command_queue\nSUBMODULE: scheduler",
            },
            {
                "kind": "ICD",
                "name": "DMA ICD",
                "version": "1.0",
                "content": "ready/valid 接口。",
            },
        ],
        "disabled_agents": disabled_agents or [],
        "custom_reviewers": custom_reviewers or [],
        "max_discussion_rounds": max_discussion_rounds,
    }


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_diagnostics_are_local_and_actionable() -> None:
    response = client.get("/api/v1/diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "warning", "error"}
    checks = {check["check_id"]: check for check in payload["checks"]}
    assert checks["backend_api"]["status"] == "pass"
    assert "default_config" in checks
    assert "saved_config" in checks
    assert "model_runtime" in checks
    assert "data_directory" in checks


def test_unhandled_configuration_error_returns_actionable_payload(monkeypatch) -> None:
    def fail_to_read_settings():
        raise SecretProtectionError("无法解密测试 Key")

    monkeypatch.setattr(model_settings_service, "public_view", fail_to_read_settings)
    safe_client = TestClient(app, raise_server_exceptions=False)
    response = safe_client.get("/api/v1/settings/model")

    assert response.status_code == 500
    payload = response.json()["error"]
    assert payload["code"] == "MODEL_KEY_UNREADABLE"
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
    assert payload["suggestions"]


def test_create_ds_task_delivers_markdown() -> None:
    response = client.post("/api/v1/tasks/ds", json=_request())
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "delivered"
    assert "# dma_ctrl 详细设计说明" in payload["ds_markdown"]
    assert "command_queue" in payload["ds_markdown"]
    assert len(payload["findings"]) == 4


def test_conflict_pauses_and_resumes() -> None:
    response = client.post("/api/v1/tasks/ds", json=_request(conflict=True))
    assert response.status_code == 201
    waiting = response.json()
    assert waiting["status"] == "waiting_for_user"
    assert waiting["pending_user_request"]["type"] == "architecture_resolution_required"
    assert waiting["issues"][0]["discussion_round"] == 2

    resumed = client.post(
        f"/api/v1/tasks/{waiting['task_id']}/resume",
        json={
            "action": "provide_resolution",
            "content": "真实架构师确认以 ICD 为准。",
            "evidence_reference": "ISSUE-123",
        },
    )
    assert resumed.status_code == 200
    delivered = resumed.json()
    assert delivered["status"] == "delivered"
    assert "ISSUE-123" in delivered["ds_markdown"]


def test_disabling_agent_adds_quality_warning() -> None:
    response = client.post(
        "/api/v1/tasks/ds",
        json=_request(disabled_agents=["verification_reviewer"]),
    )
    assert response.status_code == 201
    payload = response.json()
    assert len(payload["findings"]) == 3
    assert any("verification_reviewer" in item for item in payload["quality_warnings"])


def test_all_core_agents_and_reviewers_emit_events() -> None:
    response = client.post("/api/v1/tasks/ds", json=_request())
    assert response.status_code == 201
    payload = response.json()
    event_agents = {
        event["agent_id"] for event in payload["events"] if event["agent_id"] is not None
    }
    assert {
        "architecture_advisor",
        "design",
        "verification",
        "spec_reviewer",
        "microarchitecture_reviewer",
        "verification_reviewer",
        "interface_reviewer",
    } <= event_agents


def test_custom_reviewer_is_added() -> None:
    prompt = "从高吞吐和 backpressure 角度评审。"
    response = client.post(
        "/api/v1/tasks/ds",
        json=_request(custom_reviewers=[prompt]),
    )
    assert response.status_code == 201
    payload = response.json()
    assert len(payload["findings"]) == 5
    assert payload["findings"][-1]["reviewer"] == "custom_reviewer_1"
    assert prompt in payload["findings"][-1]["description"]


def test_discussion_round_limit_is_configurable() -> None:
    response = client.post(
        "/api/v1/tasks/ds",
        json=_request(conflict=True, max_discussion_rounds=1),
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "waiting_for_user"
    assert payload["issues"][0]["discussion_round"] == 1


def test_accepting_provisional_assumption_resumes_task() -> None:
    response = client.post("/api/v1/tasks/ds", json=_request(conflict=True))
    waiting = response.json()
    resumed = client.post(
        f"/api/v1/tasks/{waiting['task_id']}/resume",
        json={
            "action": "accept_assumption",
            "content": "用户允许按临时假设继续。",
        },
    )
    assert resumed.status_code == 200
    payload = resumed.json()
    assert payload["status"] == "delivered"
    assert "用户允许按临时假设继续" in payload["ds_markdown"]


def test_user_can_cancel_waiting_task() -> None:
    response = client.post("/api/v1/tasks/ds", json=_request(conflict=True))
    waiting = response.json()
    cancelled = client.post(
        f"/api/v1/tasks/{waiting['task_id']}/resume",
        json={"action": "cancel", "content": ""},
    )
    assert cancelled.status_code == 200
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["ds_markdown"] is None


def test_get_task_returns_latest_state() -> None:
    created = client.post("/api/v1/tasks/ds", json=_request()).json()
    fetched = client.get(f"/api/v1/tasks/{created['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_get_unknown_task_returns_404() -> None:
    response = client.get("/api/v1/tasks/not-a-real-task")
    assert response.status_code == 404


def test_delivered_task_cannot_be_resumed() -> None:
    created = client.post("/api/v1/tasks/ds", json=_request()).json()
    response = client.post(
        f"/api/v1/tasks/{created['task_id']}/resume",
        json={"action": "ignore", "content": ""},
    )
    assert response.status_code == 409


def test_disabling_design_agent_uses_minimal_template() -> None:
    response = client.post(
        "/api/v1/tasks/ds",
        json=_request(disabled_agents=["design"]),
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "delivered"
    assert "dma_ctrl_core" in payload["ds_markdown"]
    assert any("design" in item for item in payload["quality_warnings"])


def test_invalid_discussion_round_limit_is_rejected() -> None:
    response = client.post(
        "/api/v1/tasks/ds",
        json=_request(max_discussion_rounds=4),
    )
    assert response.status_code == 422


def test_as_document_is_required() -> None:
    body = _request()
    body["documents"] = [document for document in body["documents"] if document["kind"] != "AS"]
    response = client.post("/api/v1/tasks/ds", json=body)
    assert response.status_code == 422


def test_markdown_upload_is_extracted_and_classified() -> None:
    response = client.post(
        "/api/v1/documents/extract",
        files={"file": ("dma_AS.md", "# DMA AS\nSUBMODULE: scheduler", "text/markdown")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "dma_AS.md"
    assert payload["suggested_kind"] == "AS"
    assert "scheduler" in payload["content"]


def test_unsupported_upload_is_rejected() -> None:
    response = client.post(
        "/api/v1/documents/extract",
        files={"file": ("wave.vcd", b"not-a-document", "application/octet-stream")},
    )
    assert response.status_code == 422


def test_document_kind_inference_uses_filename_tokens() -> None:
    response = client.post(
        "/api/v1/documents/extract",
        files={"file": ("basic_notes.md", "design notes", "text/markdown")},
    )
    assert response.status_code == 200
    assert response.json()["suggested_kind"] is None


def test_chat_without_documents_asks_for_upload() -> None:
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": "demo-project",
            "module_name": "dma_ctrl",
            "message": "帮我生成 DS",
            "documents": [],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["task"] is None
    assert "上传" in payload["assistant_message"]
    assert payload["route"] == "orchestrated"
    assert payload["run"]["result"]["tool_calls"][0]["tool_name"] == "create_ds_candidate"


def test_chat_with_as_uses_ds_tool() -> None:
    body = _request()
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": body["project_id"],
            "module_name": body["module_name"],
            "message": "请生成 DS 并进行多视角评审",
            "documents": body["documents"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["task"]["status"] == "delivered"
    assert "DS 候选已经生成" in payload["assistant_message"]
    assert payload["route"] == "orchestrated"
    assert payload["run"]["result"]["intent"]["raw_request"] == "请生成 DS 并进行多视角评审"


def test_general_chat_does_not_require_documents() -> None:
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": "demo-project",
            "module_name": "dma_ctrl",
            "message": "这个系统能做什么？",
            "documents": [],
            "history": [],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "orchestrated"
    assert payload["run"]["run_type"] == "chat"
    assert payload["run"]["result"]["tool_calls"][0]["tool_name"] == "reply_to_user"
    assert payload["task"] is None
    assert "普通技术问答" in payload["assistant_message"]
    assert "请先上传" not in payload["assistant_message"]


def test_document_question_routes_to_chat_with_context() -> None:
    body = _request()
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": body["project_id"],
            "module_name": body["module_name"],
            "message": "这些文档分别描述了什么？",
            "documents": body["documents"],
            "history": [
                {"role": "user", "content": "我们先讨论这个模块。"},
                {"role": "assistant", "content": "好的。"},
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "orchestrated"
    assert payload["run"]["run_type"] == "chat"
    assert payload["task"] is None
    assert "DMA AS" in payload["assistant_message"]


def test_asking_what_ds_is_does_not_start_workflow() -> None:
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": "demo-project",
            "module_name": "dma_ctrl",
            "message": "DS 是什么？一般包含哪些内容？",
            "documents": [],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "orchestrated"
    assert payload["run"]["result"]["tool_calls"][0]["tool_name"] == "reply_to_user"

