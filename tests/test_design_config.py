from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api import routes
from app.config.model_settings import ModelEndpointSettings
from app.domain.design_config import ConfigUpdate
from app.domain.enums import AgentRole, UserRole
from app.domain.models import ChatRequest, PatchCreateRequest, RunResumeRequest, WorkspaceCreateRequest
from app.services.config_completion_service import ConfigCompletionService
from app.services.config_extractor_service import ConfigExtractorService
from app.services.config_service import DESIGN_CONFIG_PATH, ConfigRevisionConflict, ConfigService
from app.services.context_builder_service import ContextBuilderService
from app.services.conversation_service import conversation_service
from app.services import design_data_model
from app.services.delegation_service import DelegationService
from app.services.document_service import DocumentService
from app.services.memory_extractor_service import MemoryExtractorService, memory_extractor_service
from app.services.memory_service import MemoryService
from app.services.orchestrator_service import OrchestratorService, orchestrator_service
from app.services.patch_service import PatchService
from app.services.rtl_service import RTLService
from app.services.run_service import RunService
from app.services.session_service import SessionService
from app.services.workspace_service import WorkspaceService
from app.storage.database import Database


@pytest.fixture
def config_system(tmp_path):
    db = Database(tmp_path / "config.db")
    sessions = SessionService(db)
    workspaces = WorkspaceService(db, tmp_path / "workspaces")
    return ConfigService(db, workspaces), sessions


def test_production_extractors_use_only_the_authorized_deepseek_endpoint(monkeypatch):
    endpoint = ModelEndpointSettings(
        enabled=True, provider="deepseek", api_style="responses", model="deepseek-flash",
        base_url="https://api.deepseek.com", api_key="synthetic-test-key",
        timeout_seconds=60, max_output_tokens=1000, api_key_source="test",
    )
    monkeypatch.setenv("CHIP_AGENT_RUNTIME_MODE", "production")
    monkeypatch.setattr(design_data_model.model_settings_service, "effective",
                        lambda: SimpleNamespace(main_endpoint=lambda: endpoint))
    calls = []

    def get_provider(settings, *, direct_only=False):
        assert settings.base_url == "https://api.deepseek.com" and direct_only

        def generate(prompt, payload, **kwargs):
            calls.append((payload, kwargs))
            return {}

        return SimpleNamespace(generate_json=generate)

    monkeypatch.setattr(design_data_model.provider_factory, "get", get_provider)
    config = orchestrator_service.config_extractor.generate
    memory = memory_extractor_service.generate_memories
    config("configuration test prompt", {"current_request": "synthetic config request"})
    memory("memory test prompt", {"user_request": "synthetic Run"})
    assert [kwargs["expected_shape"] for _, kwargs in calls] == ['{"updates":[]}', '{"memories":[]}']
    assert all(not kwargs["repair_json"] for _, kwargs in calls)

    for base_url in ("http://api.deepseek.com", "https://api.deepseek.com.example.test", "https://proxy.example.test"):
        endpoint = replace(endpoint, base_url=base_url)
        with pytest.raises(RuntimeError, match="authorized only"):
            memory("memory test prompt", {"user_request": "synthetic Run"})
    endpoint = replace(endpoint, base_url="https://api.deepseek.com", enabled=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        config("configuration test prompt", {})
    assert len(calls) == 2


def _request(config_system, body, *, project="chip", module="uart", session_id=None, workspace_id=None):
    _, sessions = config_system
    session_id = session_id or str(uuid4())
    sessions.resolve(session_id, project, UserRole.DESIGN_ENGINEER, module)
    message = sessions.add_message(session_id, "user", body, agent_role="design")
    return ChatRequest(
        session_id=session_id, project_id=project, module_name=module, message=body, workspace_id=workspace_id,
    ), message


def _update(request, parameter, value, instance=None):
    return {
        "module": request.module_name, "instance": instance, "parameter": parameter,
        "value": value, "evidence": request.message,
    }


def _apply(config_system, request, source, updates, baseline=None):
    configs, _ = config_system
    return configs.apply_updates(
        request.project_id, request.module_name, [ConfigUpdate.model_validate(item) for item in updates],
        source=source, baseline=baseline or configs.get_config(request.project_id, request.module_name),
        workspace_id=request.workspace_id,
    )


def test_config_extraction_uses_confirmed_current_request_and_three_visible_complete_turns(config_system):
    configs, sessions = config_system
    first, source = _request(config_system, "把 FIFO 深度设为32")
    calls = []

    def generate(prompt, payload):
        calls.append(payload)
        assert "禁止把旧描述或助手建议当作当前的用户确认" in prompt
        if payload["current_request"].endswith("？"):
            return {"updates": []}
        value = 32 if len(calls) == 1 else 64
        return {"updates": [{
            "module": "uart", "parameter": "FIFO-Depth", "value": value,
            "evidence": payload["current_request"],
        }]}

    extractor = ConfigExtractorService(configs, sessions, generate)
    snapshot = extractor.prepare(first, source, "design")
    initial = snapshot.modules["uart"].parameters["fifo_depth"]
    assert initial.revision == 1 and initial.status == "pending" and initial.source_message_id == source.message_id
    [workspace] = configs.workspaces.list("chip")
    assert configs.file_path(workspace) == configs.workspaces.resolve_path(workspace.workspace_id, DESIGN_CONFIG_PATH)
    assert configs.file_path(workspace).exists()

    for index in range(5):
        _, user = _request(config_system, f"Review FIFO option {index}", session_id=first.session_id)
        sessions.add_message(first.session_id, "assistant", f"Option {index} means FIFO depth.",
                             agent_role="design", turn_id=user.turn_id)
    private = sessions.add_message(first.session_id, "user", "Private verification question", agent_role="verification")
    sessions.add_message(first.session_id, "assistant", "Private elaboration", agent_role="verification", turn_id=private.turn_id)
    request, message = _request(config_system, "改成64", session_id=first.session_id)
    changed = extractor.prepare(request, message, "design")
    assert [turn["user"] for turn in calls[-1]["recent_turns"]] == [f"Review FIFO option {i}" for i in range(2, 5)]
    assert calls[-1]["current_config"]["parameters"]["fifo_depth"]["value"] == 32
    current = changed.modules["uart"].parameters["fifo_depth"]
    assert current.value == 64 and current.revision == 2
    assert current.history[0].value == 32 and current.config_item_id == initial.config_item_id
    question, asked = _request(config_system, "FIFO 深度改成128可以吗？", session_id=first.session_id)
    assert extractor.prepare(question, asked, "design").revision == 2


@pytest.mark.parametrize("mode", ["invalid_shape", "wrong_module", "invented_evidence", "provider_failure"])
def test_config_rejects_invalid_batch_without_writing_any_item(config_system, mode):
    configs, sessions = config_system
    request, source = _request(config_system, "将 FIFO 深度改成64")

    def generate(prompt, payload):
        item = _update(request, "fifo_depth", 64)
        if mode == "invalid_shape":
            invalid = {"path": "../outside.json"}
        elif mode == "wrong_module":
            invalid = {**item, "module": "spi"}
        elif mode == "invented_evidence":
            invalid = {**item, "evidence": "bogus source"}
        else:
            raise RuntimeError("model unavailable")
        return {"updates": [item, invalid]}

    assert ConfigExtractorService(configs, sessions, generate).prepare(request, source, "design").revision == 0
    assert not configs.workspaces.list("chip")


def test_concurrent_disjoint_patches_preserve_updates_and_stale_same_item_is_rejected(config_system):
    configs, _ = config_system
    first = _request(config_system, "Set FIFO depth to 64")
    second = _request(config_system, "Set data width to 8")
    baseline = configs.get_config("chip", "uart")
    barrier = threading.Barrier(2)

    def change(pair, parameter, value):
        request, source = pair
        barrier.wait(timeout=8)
        return _apply(config_system, request, source, [_update(request, parameter, value)], baseline)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(change, *args) for args in ((first, "fifo_depth", 64), (second, "data_width", 8))]
        for future in jobs:
            future.result(timeout=12)
    config = configs.get_config("chip", "uart")
    assert config.revision == 2
    assert set(config.modules["uart"].parameters) == {"fifo_depth", "data_width"}
    assert len(configs.workspaces.list("chip")) == 1
    request, source = _request(config_system, "Set FIFO depth to 128")
    with pytest.raises(ConfigRevisionConflict):
        _apply(config_system, request, source, [_update(request, "fifo_depth", 128)], baseline)
    assert configs.get_config("chip").revision == 2

    spi, spi_source = _request(config_system, "Set FIFO depth to 4", module="spi")
    _apply(config_system, spi, spi_source, [_update(spi, "fifo_depth", 4), _update(spi, "fifo_depth", 7, "rx")])
    config = configs.get_config("chip")
    assert config.modules["uart"].get(None, "fifo_depth").value == 64
    assert config.modules["spi"].get(None, "fifo_depth").value == 4
    assert config.modules["spi"].get("rx", "fifo_depth").value == 7
    with pytest.raises(ValueError, match="Workspace"):
        configs.get_config("other", workspace_id=config.workspace_id)
    with pytest.raises(ValueError, match="ConfigService"):
        configs.workspaces.write_text(config.workspace_id, DESIGN_CONFIG_PATH, "{}")


def _run(config_system, request, snapshot):
    configs, _ = config_system
    patches = PatchService(configs.workspaces, configs.db)
    runs = RunService(
        db=configs.db, workspaces=configs.workspaces, patches=patches,
        rtl=RTLService(configs.workspaces, patches),
    )
    request = request.model_copy(update={"workspace_id": snapshot.workspace_id})
    run_id = runs.create_orchestration_run(request.session_id, request, {}, AgentRole.DESIGN)
    configs.record_input(run_id, snapshot)
    return runs, run_id


def test_completion_uses_applied_patch_and_revision_fence(config_system):
    from app.domain.models import PatchDecisionRequest

    configs, _ = config_system
    request, source = _request(config_system, "Set FIFO depth to 64")
    pinned = _apply(config_system, request, source, [_update(request, "fifo_depth", 64)])
    runs, run_id = _run(config_system, request, pinned)
    completion = ConfigCompletionService(configs)
    patch = runs.patches.create(PatchCreateRequest(
        workspace_id=pinned.workspace_id, target_path="rtl/uart.sv",
        after_content="module uart #(parameter int FIFO_DEPTH = 64)(); endmodule", reason="Implement specification",
    ))
    configs.db.execute("UPDATE runs SET patch_id = ?, status = 'waiting_for_approval' WHERE run_id = ?", (patch.patch_id, run_id))
    completion.complete(run_id)
    assert configs.get_pending_items("chip", "uart")
    runs.patches.decide(patch.patch_id, PatchDecisionRequest(action="approve"))
    completion.complete(run_id)
    assert configs.get_pending_items("chip", "uart")

    runs.complete_reply(run_id, "Claim without an engineer receipt")
    completion.complete(run_id)
    implemented = configs.get_config("chip", "uart").modules["uart"].get(None, "fifo_depth")
    assert implemented.status == "implemented" and implemented.evidence_refs[0].source_id == patch.patch_id
    completion.complete(run_id)
    assert len(configs.run_config(run_id, final=True).modules["uart"].get(None, "fifo_depth").evidence_refs) == 1
    identical = _apply(config_system, request, source, [_update(request, "fifo_depth", 64)])
    assert identical.revision == pinned.revision and identical.modules["uart"].get(None, "fifo_depth").status == "implemented"

    old_runs, old_id = _run(config_system, request, pinned)
    configs.db.execute("UPDATE runs SET patch_id = ? WHERE run_id = ?", (patch.patch_id, old_id))
    second, new_source = _request(config_system, "Set FIFO depth to 128")
    _apply(config_system, second, new_source, [_update(second, "fifo_depth", 128)])
    old_runs.complete_reply(old_id, "Finished the original design")
    completion.complete(old_id)
    latest = configs.get_config("chip").modules["uart"].get(None, "fifo_depth")
    assert (latest.value, latest.revision, latest.status) == (128, 2, "pending")
    assert configs.run_config(old_id, final=True).modules["uart"].get(None, "fifo_depth").value == 64

    no_evidence, new_id = _run(config_system, second, configs.get_config("chip", "uart"))
    no_evidence.complete_reply(new_id, "FIFO_DEPTH=128 implemented and verified")
    completion.complete(new_id)
    assert configs.get_pending_items("chip", "uart")


@pytest.mark.asyncio
@pytest.mark.parametrize("hierarchical", [False, True])
async def test_chat_entrypoints_pin_config_and_worker_skips_config_duplicates(config_system, monkeypatch, hierarchical):
    configs, sessions = config_system
    db = configs.db
    memories = MemoryService(db)
    documents = DocumentService(db, memories=memories)
    contexts = ContextBuilderService(db, sessions, documents, memories)
    patches = PatchService(configs.workspaces, db)
    run_service = RunService(db=db, workspaces=configs.workspaces, patches=patches, rtl=RTLService(configs.workspaces, patches))
    read_config, started, release = threading.Event(), threading.Event(), threading.Event()
    received = []

    def generate_memory(prompt, payload):
        received.append(payload)
        started.set()
        assert release.wait(8)
        return {"memories": [
            {"action": "add", "content": "FIFO depth is 64."},
            {"action": "add", "content": "Always measure peak FIFO throughput before deciding a design."},
        ]}

    worker = MemoryExtractorService(db, memories, sessions, generate_memories=generate_memory, configs=configs)
    build_extraction = worker.contexts.build

    def delayed_context(run):
        assert read_config.wait(8)
        return build_extraction(run)

    monkeypatch.setattr(worker.contexts, "build", delayed_context)
    extractor = ConfigExtractorService(configs, sessions, lambda prompt, payload: {"updates": [{
        "module": "uart", "parameter": "fifo_depth", "value": 64, "evidence": payload["current_request"],
    }]})
    orchestrator = OrchestratorService(
        runs=run_service, sessions=sessions, contexts=contexts, delegations=DelegationService(db, memories),
        memory_extractor=worker, config_extractor=extractor,
    )
    if hierarchical:
        from test_hierarchical_run import ScriptedRuntime

        monkeypatch.setenv("CHIP_AGENT_HIERARCHICAL_ENABLED", "1")
        orchestrator.hierarchical.runtime = ScriptedRuntime()

    async def handle(request):
        return await orchestrator.handle(request)

    monkeypatch.setattr(conversation_service, "handle", handle)
    try:
        response = await routes.chat(ChatRequest(
            project_id="chip", module_name="uart", message="介绍 UART。请将 FIFO 深度设为64。",
            orchestration_mode="chat" if hierarchical else None,
        ))
        assert response.run.status.value == "completed"
        assert not started.is_set()
        [manifest, *_] = contexts.list_for_run(response.run.run_id)
        assert manifest.sections["project_config"]["revision"] == 1
        assert manifest.sections["project_config"]["tokens"] > 0

        changed, source = _request(config_system, "Set FIFO depth to 128")
        _apply(config_system, changed, source, [_update(changed, "fifo_depth", 128)])
        snapshot = configs.run_config(response.run.run_id, final=True)
        assert snapshot.revision == 1 and snapshot.modules["uart"].get(None, "fifo_depth").value == 64
        read_config.set()
        assert started.wait(5)
        assert received[0]["project_config"]["parameters"]["fifo_depth"]["value"] == 64

        release.set()
        deadline = monotonic() + 6
        while monotonic() < deadline:
            row = db.fetch_one("SELECT status FROM memory_extraction_events WHERE source_id = ?", (response.run.run_id,))
            if row and row["status"] == "completed":
                break
            sleep(0.02)
        else:
            pytest.fail("Memory event did not complete")
        [memory] = memories.list("chip")
        assert memory.kind == "semantic" and "peak FIFO throughput" in memory.content
    finally:
        read_config.set()
        release.set()
        worker._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_legacy_resume_checks_status_before_delivering_memory(config_system, monkeypatch):
    configs, sessions = config_system
    request, source = _request(config_system, "Set FIFO depth to 64")
    pinned = _apply(config_system, request, source, [_update(request, "fifo_depth", 64)])
    runs, run_id = _run(config_system, request, pinned)
    patch = runs.patches.create(PatchCreateRequest(
        workspace_id=pinned.workspace_id, target_path="rtl/uart.sv",
        after_content="module uart; parameter FIFO_DEPTH = 64; endmodule", reason="Apply FIFO target",
    ))
    configs.db.execute(
        "UPDATE runs SET run_type='rtl_implementation', status='waiting_for_approval', patch_id=? WHERE run_id=?",
        (patch.patch_id, run_id),
    )
    enqueued = []

    def enqueue_run(**event):
        final = configs.run_config(event["run_id"], final=True)
        assert final.modules["uart"].get(None, "fifo_depth").status == "implemented"
        assert sessions.run_messages(request.session_id, run_id, "design")[-1].role == "assistant"
        enqueued.append(event)

    orchestrator = OrchestratorService(
        runs=runs, sessions=sessions, memory_extractor=SimpleNamespace(enqueue_run=enqueue_run),
        config_extractor=ConfigExtractorService(configs, sessions),
    )
    monkeypatch.setattr(routes, "run_service", runs)
    monkeypatch.setattr(routes, "session_service", sessions)
    monkeypatch.setattr(routes, "orchestrator_service", orchestrator)
    monkeypatch.setattr(routes.conversation_summary_service, "enqueue_turn", lambda *args: None)

    result = await routes.resume_run(run_id, RunResumeRequest(action="approve"))
    assert result.status.value == "completed"
    assert len(enqueued) == 1
