"""v0.6 上下文、RAG、后端会话与长期记忆回归测试。"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_chat_is_persisted_and_context_manifest_is_auditable() -> None:
    project_id = f"context-{uuid4()}"
    response = client.post(
        "/api/v1/chat",
        json={
            "project_id": project_id,
            "module_name": "dma_ctrl",
            "message": "你好，请解释 ready/valid 握手。",
            "history": [],
            "documents": [],
            "user_role": "design_engineer",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    conversations = client.get(
        "/api/v1/conversations", params={"project_id": project_id}
    ).json()
    assert conversations[0]["session_id"] == payload["session_id"]
    assert conversations[0]["message_count"] == 2

    messages = client.get(
        f"/api/v1/conversations/{payload['session_id']}/messages"
    ).json()["items"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "ready/valid" in messages[0]["content"]

    manifests = client.get(
        f"/api/v1/runs/{payload['run']['run_id']}/contexts"
    ).json()
    assert len(manifests) == 1
    assert manifests[0]["token_budget"] > 0
    assert "documents" in manifests[0]["sections"]


def test_hybrid_retrieval_matches_cross_language_engineering_concept() -> None:
    project_id = f"rag-{uuid4()}"
    stored = client.post(
        "/api/v1/documents",
        json={
            "project_id": project_id,
            "module_name": "scheduler",
            "kind": "AS",
            "name": "Scheduler AS",
            "version": "1.0",
            "content": "# 流控\n当下游 ready 拉低时，命令通道必须保持 valid 和 payload，形成背压。",
        },
    )
    assert stored.status_code == 201, stored.text

    result = client.post(
        "/api/v1/retrieval/search",
        json={
            "project_id": project_id,
            "module_name": "scheduler",
            "query": "How should backpressure work on the command interface?",
            "top_k": 3,
        },
    )
    assert result.status_code == 200, result.text
    chunks = result.json()["chunks"]
    assert chunks
    assert "背压" in chunks[0]["content"]
    assert chunks[0]["semantic_score"] > 0


def test_new_document_version_marks_dependent_memory_stale() -> None:
    project_id = f"memory-{uuid4()}"
    first = client.post(
        "/api/v1/documents",
        json={
            "project_id": project_id,
            "module_name": "dma_ctrl",
            "kind": "AS",
            "name": "DMA AS",
            "version": "1.0",
            "content": "完成状态通过 done 脉冲上报。",
        },
    ).json()
    memory = client.post(
        "/api/v1/memories",
        json={
            "project_id": project_id,
            "module_name": "dma_ctrl",
            "kind": "fact",
            "content": "完成状态使用 done 脉冲。",
            "confidence": 10,
            "authority": "AS",
            "status": "active",
            "source_refs": [
                {
                    "source_type": "document_version",
                    "source_id": first["version_id"],
                    "source_version": "1.0",
                }
            ],
        },
    )
    assert memory.status_code == 201, memory.text

    second = client.post(
        "/api/v1/documents",
        json={
            "project_id": project_id,
            "module_name": "dma_ctrl",
            "kind": "AS",
            "name": "DMA AS",
            "version": "2.0",
            "content": "完成状态改为 completion_valid/completion_ready 握手上报。",
        },
    )
    assert second.status_code == 201, second.text

    memories = client.get(
        "/api/v1/memories", params={"project_id": project_id, "module_name": "dma_ctrl"}
    ).json()
    assert memories[0]["status"] == "stale"
    versions = client.get(
        "/api/v1/documents", params={"project_id": project_id, "module_name": "dma_ctrl"}
    ).json()
    assert {version["status"] for version in versions} == {"active", "superseded"}


def test_active_fact_without_source_is_rejected() -> None:
    response = client.post(
        "/api/v1/memories",
        json={
            "project_id": f"source-{uuid4()}",
            "module_name": "dma_ctrl",
            "kind": "fact",
            "content": "这条事实没有来源。",
            "status": "active",
            "source_refs": [],
        },
    )
    assert response.status_code == 422
