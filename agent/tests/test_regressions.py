from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import ValidationError
from test_agent import make_client, make_settings

from audience_agent.api import Runtime, create_app
from audience_agent.contracts import EvidenceRecord, GroundedAnswer
from audience_agent.graph import build_graph, classify_intent
from audience_agent.llm import DeterministicMockModel
from audience_agent.repository import (
    DatasetVersionConflict,
    InMemoryEvidenceRepository,
    PostgresEvidenceRepository,
)
from audience_agent.seeding import load_seed


def test_active_ingest_returns_409_and_preserves_immutable_record():
    client, repo = make_client()
    settings = make_settings()
    original = load_seed(settings.seed_path)[0]
    changed = original.model_copy(update={"content": "unreviewed overwrite attempt"})
    with client:
        response = client.post(
            "/v1/workspaces/demo/evidence",
            headers={"X-API-Key": "test-token"},
            json=[changed.model_dump(mode="json")],
        )
        stored = asyncio.run(
            repo.get_many("demo", original.dataset_version, [original.id])
        )[0]
    assert response.status_code == 409
    assert stored.content == original.content


@pytest.mark.asyncio
async def test_active_and_retired_versions_are_immutable_in_memory():
    settings = make_settings()
    original = load_seed(settings.seed_path)[0]
    repo = InMemoryEvidenceRepository([original])
    staged = original.model_copy(update={"dataset_version": "v2", "id": "v2-record"})
    await repo.ingest([staged])
    await repo.activate_dataset("demo", "v2")
    with pytest.raises(DatasetVersionConflict):
        await repo.ingest([staged])
    with pytest.raises(DatasetVersionConflict):
        await repo.ingest([original])
    with pytest.raises(DatasetVersionConflict):
        await repo.activate_dataset("demo", original.dataset_version)


@pytest.mark.asyncio
async def test_postgres_ingest_checks_immutable_status_inside_transaction():
    class Transaction:
        entered = 0

        async def __aenter__(self):
            self.entered += 1

        async def __aexit__(self, *args):
            return False

    class Connection:
        def __init__(self):
            self.transaction_obj = Transaction()
            self.sql = []

        def transaction(self):
            return self.transaction_obj

        async def execute(self, query, *args):
            self.sql.append(query)

        async def fetchval(self, query, *args):
            assert "FOR UPDATE" in query
            return "active"

    class Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *args):
            return False

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return Acquire(self.conn)

    conn = Connection()
    repo = PostgresEvidenceRepository(Pool(conn))
    record = load_seed(make_settings().seed_path)[0]
    with pytest.raises(DatasetVersionConflict):
        await repo.ingest([record])
    assert conn.transaction_obj.entered == 1
    assert any("pg_advisory_xact_lock" in query for query in conn.sql)
    assert not any("INSERT INTO evidence_records" in query for query in conn.sql)


def test_database_migration_enforces_staged_writes_and_lifecycle():
    sql = Path("migrations/002_evidence_integrity.sql").read_text(encoding="utf-8")
    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "dataset_status <> 'staged'" in sql
    assert "TG_OP IN ('UPDATE', 'DELETE')" in sql
    assert "TG_OP IN ('INSERT', 'UPDATE')" in sql
    assert "OLD.workspace_id AND dataset_version = OLD.dataset_version" in sql
    assert "NEW.workspace_id AND dataset_version = NEW.dataset_version" in sql
    assert "source evidence dataset" in sql
    assert "target evidence dataset" in sql
    assert "retired dataset versions cannot be reactivated" in sql
    assert "CREATE TABLE IF NOT EXISTS insight_runs" in sql


@pytest.mark.parametrize("field", ["id", "workspace_id", "dataset_version", "source_material"])
def test_ingestion_rejects_pii_in_previously_unchecked_fields(field):
    payload = load_seed(make_settings().seed_path)[0].model_dump(mode="json")
    payload[field] = "leak-13812345678"
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate(payload)


@pytest.mark.parametrize("identifier", ["13812345678", "person@example.com", "110101199003071234"])
def test_workspace_grant_rejects_direct_identifiers(identifier):
    settings = make_settings(
        api_keys_json=json.dumps(
            {"test-token": {"workspace": identifier, "roles": ["insight:read"]}}
        )
    )
    with pytest.raises(ValueError, match="direct personal identifiers"):
        settings.api_key_map()


@pytest.mark.parametrize("identifier", ["13812345678", "person@example.com", "110101199003071234"])
def test_workspace_path_rejects_direct_identifiers_without_recording_run(identifier):
    client, repo = make_client()
    with client:
        response = client.post(
            f"/v1/workspaces/{identifier}/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播ROI如何计算？"},
        )
    assert response.status_code == 422
    assert identifier not in response.text
    assert repo.runs == {}


class LimitationModel:
    def __init__(self, count: int, include_pii: bool = False):
        self.count = count
        self.include_pii = include_pii

    async def synthesize(self, question, evidence, *, deadline=None):
        limitations = [f"ordinary limitation {index}" for index in range(self.count)]
        if self.include_pii:
            limitations = ["ordinary limitation", "leak 13812345678", "ordinary limitation"]
        return GroundedAnswer(
            claims=[
                {
                    "text": "invalid citation",
                    "kind": "observation",
                    "evidence_ids": ["NOT-RETRIEVED"],
                    "confidence": "low",
                }
            ],
            limitations=limitations,
            next_steps=[],
        ), {"model": "test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [4, 5, 6])
async def test_limitations_never_duplicate_or_exceed_six(count):
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, LimitationModel(count), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": "直播ROI如何计算？", "run_id": str(uuid4())},
        config={"configurable": {"thread_id": f"demo:limitations-{count}"}},
    )
    limitations = result["answer"]["limitations"]
    assert len(limitations) <= 6
    assert len(limitations) == len(set(limitations))
    assert any("丢弃" in text for text in limitations)


@pytest.mark.asyncio
async def test_limitations_remove_pii_without_repeating_safe_text():
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, LimitationModel(2, include_pii=True), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": "面料体验问题有哪些？", "run_id": str(uuid4())},
        config={"configurable": {"thread_id": "demo:limitations-pii"}},
    )
    limitations = result["answer"]["limitations"]
    assert "13812345678" not in "".join(limitations)
    assert limitations.count("ordinary limitation") == 1


@pytest.mark.asyncio
async def test_mandatory_safety_cross_domain_and_m5_limits_survive_capacity_pressure():
    class AdversarialLimitationModel:
        async def synthesize(self, question, evidence, *, deadline=None):
            m2 = next(item for item in evidence if item.source_material == "M2")
            m3 = next(item for item in evidence if item.source_material == "M3")
            m5 = next(item for item in evidence if item.source_material == "M5")
            return GroundedAnswer(
                claims=[
                    {
                        "text": "有证据支持的跨域观察。",
                        "kind": "observation",
                        "evidence_ids": [m2.id],
                        "confidence": "high",
                    },
                    {
                        "text": "无来源主张。",
                        "kind": "observation",
                        "evidence_ids": ["NOT-RETRIEVED"],
                        "confidence": "high",
                    },
                    {
                        "text": f"用户电话13812345678，引自{m3.id}。",
                        "kind": "observation",
                        "evidence_ids": [m3.id],
                        "confidence": "high",
                    },
                    {
                        "text": "M5已经证明确定的人群事实。",
                        "kind": "observation",
                        "evidence_ids": [m5.id],
                        "confidence": "high",
                    },
                ],
                limitations=[f"model limitation {index}" for index in range(4)],
            ), {"model": "test"}

    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, AdversarialLimitationModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": JOINT_QUESTION, "run_id": str(uuid4())},
        config={"configurable": {"thread_id": "demo:mandatory-limitations"}},
    )
    assert result["status"] == "answered"
    limitations = result["answer"]["limitations"]
    assert len(limitations) <= 6
    assert any("未绑定本次检索证据" in item for item in limitations)
    assert any("直接个人标识符" in item for item in limitations)
    assert any("M5观点误述为事实" in item for item in limitations)
    assert any("跨域证据口径" in item for item in limitations)
    assert any("M5仅为观点或待验证假设" in item for item in limitations)


def test_feedback_rejects_unknown_run_wrong_thread_and_wrong_dataset():
    client, repo = make_client()
    with client:
        insight = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "仓库工单和补发订单比例是多少？"},
        ).json()
        base = {
            "run_id": insight["run_id"],
            "thread_id": insight["thread_id"],
            "dataset_version": insight["dataset_version"],
            "decision": "needs_validation",
            "notes": "保持decision/notes契约。",
        }
        unknown = client.post(
            "/v1/workspaces/demo/feedback",
            headers={"X-API-Key": "test-token"},
            json={**base, "run_id": str(uuid4())},
        )
        wrong_thread = client.post(
            "/v1/workspaces/demo/feedback",
            headers={"X-API-Key": "test-token"},
            json={**base, "thread_id": "wrong-thread-id"},
        )
        wrong_dataset = client.post(
            "/v1/workspaces/demo/feedback",
            headers={"X-API-Key": "test-token"},
            json={**base, "dataset_version": "not-the-run-version"},
        )
    assert unknown.status_code == 422
    assert wrong_thread.status_code == 422
    assert wrong_dataset.status_code == 422
    assert repo.feedback == []


def test_readiness_checks_storage_checkpoint_and_model_adapter():
    client, _ = make_client()
    with client:
        runtime = client.app.state.runtime

        async def not_ready():
            return False

        original_health = runtime.repository.health
        runtime.repository.health = not_ready
        storage_response = client.get("/readyz")
        runtime.repository.health = original_health
        runtime.checkpoint_health = not_ready
        checkpoint_response = client.get("/readyz")
        runtime.checkpoint_health = None
        runtime.model_ready = False
        model_response = client.get("/readyz")
    assert storage_response.status_code == 503
    assert checkpoint_response.status_code == 503
    assert model_response.status_code == 503


def test_total_graph_deadline_returns_controlled_504():
    class SlowModel:
        async def synthesize(self, question, evidence, *, deadline=None):
            await asyncio.sleep(5)

    client, _ = make_client(model=SlowModel(), request_timeout_seconds=0.06)
    started = time.monotonic()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播ROI如何计算？"},
        )
    assert response.status_code == 504
    assert time.monotonic() - started < 2


def test_uvloop_real_asgi_request_does_not_use_monotonic_clock_as_loop_deadline():
    import uvloop

    async def exercise():
        settings = make_settings()
        repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
        graph = build_graph(repo, DeterministicMockModel(), settings).compile(
            checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        )
        runtime = Runtime(repo, graph)
        app = create_app(settings, runtime_override=runtime)
        app.state.runtime = runtime
        app.state.ready = True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/workspaces/demo/insights",
                headers={"X-API-Key": "test-token"},
                json={"question": "直播ROI如何计算？"},
            )
        return response

    loop = uvloop.new_event_loop()
    try:
        response = loop.run_until_complete(exercise())
    finally:
        loop.close()
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_semaphore_queue_is_bounded_by_caller_deadline(monkeypatch):
    import audience_agent.llm as llm_module

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(parse=self.parse)

        async def parse(self, **kwargs):
            raise AssertionError("model must not run while semaphore is occupied")

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeOpenAI)
    model = llm_module.OpenAICompatibleModel(
        api_key="fake",
        model="configured-model",
        base_url="https://llm.example.test/v1",
        fallback_models=(),
        max_output_tokens=100,
        max_evidence_chars=1000,
        max_concurrent=1,
        timeout_seconds=5,
    )
    evidence = load_seed(make_settings().seed_path)[0]
    await model.semaphore.acquire()
    try:
        with pytest.raises(TimeoutError):
            await model.synthesize(
                "ROI", [evidence], deadline=asyncio.get_running_loop().time() + 0.04
            )
    finally:
        model.semaphore.release()


@pytest.mark.asyncio
async def test_model_attempts_and_fallbacks_have_hard_caps(monkeypatch):
    import audience_agent.llm as llm_module

    calls = []

    class FakeResponses:
        async def parse(self, **kwargs):
            calls.append(kwargs["model"])
            raise RuntimeError("synthetic unavailable provider")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append({"client_options": kwargs})
            self.responses = FakeResponses()

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeOpenAI)
    model = llm_module.OpenAICompatibleModel(
        api_key="fake",
        model="primary",
        base_url="https://llm.example.test/v1",
        fallback_models=("f1", "f2", "f3", "f4"),
        max_output_tokens=100,
        max_evidence_chars=1000,
        max_concurrent=1,
        max_attempts=2,
        max_fallbacks=4,
    )
    with pytest.raises(RuntimeError):
        await model.synthesize("ROI", load_seed(make_settings().seed_path)[:1])
    assert calls[0]["client_options"]["max_retries"] == 0
    assert calls[1:] == ["primary", "f1"]


JOINT_QUESTION = "请联合品牌A的Gen Z画像、穿着体验、履约信号和专家观点，形成可追溯的人群假设；漾YOUNG应如何验证？"
META_FOLLOWUP = "这些假设有哪些反例或替代解释？请指出证据不足之处，并给出下周最小验证动作。"


@pytest.mark.asyncio
async def test_joint_question_and_meta_followup_return_multidomain_evidence_over_asgi():
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, DeterministicMockModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    runtime = Runtime(repo, graph)
    app = create_app(settings, runtime_override=runtime)
    app.state.runtime = runtime
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": JOINT_QUESTION, "thread_id": "joint-followup-thread"},
        )
        followup = await client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": META_FOLLOWUP, "thread_id": "joint-followup-thread"},
        )
    assert initial.status_code == 200, initial.text
    assert followup.status_code == 200, followup.text
    first = initial.json()
    second = followup.json()
    assert first["thread_id"] == second["thread_id"] == "joint-followup-thread"
    assert first["run_id"] != second["run_id"]
    for body in (first, second):
        assert body["status"] == "answered"
        assert body["intent"] == "cross_domain"
        assert body["dataset_version"] is not None
        cards = {item["id"]: item for item in body["evidence"]}
        evidence_materials = {item["source_material"] for item in cards.values()}
        assert {"M2", "M3", "M4", "M5"}.issubset(evidence_materials)
        claim_materials = {
            cards[source_id]["source_material"]
            for claim in body["claims"]
            for source_id in claim["evidence_ids"]
        }
        assert {"M2", "M3", "M4", "M5"}.issubset(claim_materials)
        assert len(body["claims"]) >= 4
    assert any("替代解释" in item for item in second["limitations"])
    assert any("下周最小验证" in item for item in second["next_steps"])


@pytest.mark.asyncio
async def test_meta_followup_inherits_single_domain_from_thread_checkpoint():
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, DeterministicMockModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    config = {"configurable": {"thread_id": "demo:single-domain-followup"}}
    await graph.ainvoke(
        {"workspace_id": "demo", "question": "直播投放ROI如何计算？", "run_id": str(uuid4())},
        config=config,
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": META_FOLLOWUP, "run_id": str(uuid4())},
        config=config,
    )
    assert result["intent"] == "live_roi"
    assert result["dataset_version"] is not None
    assert result["evidence_ids"]
    assert set(result["evidence_materials"].values()) == {"M1"}


def test_cross_domain_route_combines_m2_m3_m4_and_m5_without_factifying_opinion():
    question = "请综合M2/M3/M4/M5形成年轻人群洞察：场景、任务、体验阻力和验证动作是什么？"
    assert classify_intent(question) == "cross_domain"
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": question},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    materials = {item["source_material"] for item in body["evidence"]}
    assert {"M2", "M3", "M4", "M5"}.issubset(materials)
    assert any("单域指标" in item for item in body["limitations"])
    m5_claims = [
        claim
        for claim in body["claims"]
        if any(
            card["source_material"] == "M5" and card["id"] in claim["evidence_ids"]
            for card in body["evidence"]
        )
    ]
    assert m5_claims
    assert all(claim["kind"] == "hypothesis" for claim in m5_claims)


@pytest.mark.asyncio
async def test_m5_observation_claim_is_rejected():
    class FactifyingModel:
        async def synthesize(self, question, evidence, *, deadline=None):
            m5 = next(item for item in evidence if item.source_material == "M5")
            return GroundedAnswer(
                claims=[
                    {
                        "text": "专家访谈已经证明年轻人群有确定需求。",
                        "kind": "observation",
                        "evidence_ids": [m5.id],
                        "confidence": "high",
                    }
                ]
            ), {"model": "test"}

    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, FactifyingModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": "请综合材料形成人群洞察", "run_id": str(uuid4())},
        config={"configurable": {"thread_id": "demo:m5-observation"}},
    )
    assert result["status"] == "abstained"
    assert any("M5观点" in item for item in result["answer"]["limitations"])


def test_prototype_and_evidence_mounts_preserve_docs_and_openapi_contract():
    client, _ = make_client()
    with client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        snapshot_response = client.get("/evidence/insights.json")
        invalid_workspace = client.post(
            "/v1/workspaces/bad.workspace/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播ROI如何计算？"},
        )
    route_names = {getattr(route, "name", "") for route in client.app.routes}
    assert {"prototype", "evidence"}.issubset(route_names)
    snapshot_path = Path(__file__).resolve().parents[2] / "evidence/meeting_snapshot.json"
    if snapshot_path.is_file():
        assert snapshot_response.status_code == 200
        assert snapshot_response.json() == json.loads(snapshot_path.read_text(encoding="utf-8"))
    validation = schema["components"]["schemas"]["ValidationError"]
    assert set(validation["properties"]) == {"location", "message", "type"}
    assert invalid_workspace.status_code == 422
    assert isinstance(invalid_workspace.json()["detail"], list)
    assert set(invalid_workspace.json()["detail"][0]) == {"location", "message", "type"}
