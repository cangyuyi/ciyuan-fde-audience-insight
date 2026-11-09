from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from audience_agent.api import Runtime, create_app
from audience_agent.config import Settings
from audience_agent.contracts import GroundedAnswer
from audience_agent.graph import build_graph, classify_intent
from audience_agent.llm import DeterministicMockModel
from audience_agent.repository import InMemoryEvidenceRepository
from audience_agent.seeding import load_seed

ROOT = Path(__file__).resolve().parents[1]


def make_settings(**overrides) -> Settings:
    values = dict(
        app_env="test",
        storage_backend="memory",
        llm_mode="mock",
        database_url="postgresql://unused",
        langgraph_database_url="postgresql://unused",
        api_keys_json='{"test-token":{"workspace":"demo","roles":["insight:read","feedback:write","evidence:write"]},"reader-token":{"workspace":"demo","roles":["insight:read"]}}',
        openai_api_key="",
        openai_base_url=None,
        openai_model="",
        openai_model_fallbacks=(),
        max_question_chars=1200,
        max_evidence_items=8,
        max_evidence_chars=12000,
        max_output_tokens=1200,
        max_concurrent_llm=4,
        request_timeout_seconds=30.0,
        llm_timeout_seconds=20.0,
        llm_max_attempts=2,
        llm_max_fallbacks=1,
        seed_path=ROOT / "data/evidence.raw.json",
        migrations_path=ROOT / "migrations",
    )
    values.update(overrides)
    return Settings(**values)


def make_client(model=None, **settings_overrides):
    settings = make_settings(**settings_overrides)
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    model = model or DeterministicMockModel()
    graph = build_graph(repo, model, settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    app = create_app(settings, runtime_override=Runtime(repo, graph))
    return TestClient(app), repo


def test_intent_router_handles_supported_topics_and_abstains_unknown():
    assert classify_intent("直播投放ROI如何按场次汇总") == "live_roi"
    assert classify_intent("品牌A Gen Z人群画像是什么") == "audience_profile"
    assert classify_intent("哪些用户反馈卷边和闷热") == "experience"
    assert classify_intent("仓库工单和补发订单比例") == "fulfillment"
    assert classify_intent("竞品市场份额是多少") is None


def test_live_roi_returns_cited_and_tenant_scoped_answer():
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播投放ROI如何按实际开播日期汇总？"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered"
    assert body["intent"] == "live_roi"
    assert body["dataset_version"].startswith("m1-m6-raw-")
    assert body["evidence"]
    assert all(claim["evidence_ids"] for claim in body["claims"])
    assert body["evidence"][0]["source_material"] == "M1"


def test_workspace_key_cannot_read_other_workspace():
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/other/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播投放ROI如何按实际开播日期汇总？"},
        )
    assert response.status_code == 403


def test_unknown_question_abstains_without_fabricating_answer():
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "这个竞品品牌的市场份额和GMV是多少？"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "abstained"
    assert body["claims"] == []
    assert body["evidence"] == []


class BadCitationModel:
    async def synthesize(self, question, evidence, *, deadline=None):
        return GroundedAnswer.model_validate(
            {
                "claims": [
                    {
                        "text": "未经检索来源支持的结论",
                        "kind": "observation",
                        "evidence_ids": ["FAKE-SOURCE"],
                        "confidence": "high",
                    }
                ],
                "limitations": [],
                "next_steps": [],
            }
        ), {"input_tokens": 0, "output_tokens": 5}


@pytest.mark.asyncio
async def test_invalid_model_citations_are_removed():
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, BadCitationModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {
            "workspace_id": "demo",
            "question": "直播投放ROI如何按实际开播日期汇总？",
            "run_id": "test-run",
        },
        config={"configurable": {"thread_id": "demo:test-thread"}},
    )
    assert result["status"] == "abstained"
    assert result["answer"]["claims"] == []
    assert "丢弃" in result["answer"]["limitations"][0]


@pytest.mark.asyncio
async def test_new_dataset_is_staged_until_explicit_activation():
    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    old_version = await repo.active_dataset_version("demo")
    old_records = await repo.search("demo", old_version, "live_roi", "ROI", 8)
    assert old_records
    replacement = old_records[0].model_copy(
        update={
            "id": "M1-TEST-STAGED",
            "dataset_version": "test-next-version",
            "content": "new staged evidence version",
        }
    )
    await repo.ingest([replacement])
    assert await repo.active_dataset_version("demo") == old_version
    assert await repo.search("demo", old_version, "live_roi", "ROI", 8)
    await repo.activate_dataset("demo", "test-next-version")
    assert await repo.active_dataset_version("demo") == "test-next-version"
    assert [r.id for r in await repo.search("demo", "test-next-version", "live_roi", "ROI", 8)] == [
        "M1-TEST-STAGED"
    ]


def test_reader_key_cannot_ingest_evidence():
    client, _ = make_client()
    from audience_agent.seeding import load_seed

    settings = make_settings()
    record = load_seed(settings.seed_path)[0].model_dump()
    with client:
        response = client.post(
            "/v1/workspaces/demo/evidence",
            headers={"X-API-Key": "reader-token"},
            json=[record],
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_openai_adapter_uses_configured_model_and_structured_output(monkeypatch):
    from types import SimpleNamespace

    import audience_agent.llm as llm_module
    from audience_agent.contracts import EvidenceRecord, GroundedAnswer
    from audience_agent.llm import OpenAICompatibleModel

    calls = {}
    expected = GroundedAnswer(
        claims=[
            {
                "text": "加权口径已绑定来源。",
                "kind": "observation",
                "evidence_ids": ["M1-ROI-WEIGHTED-20260601-18"],
                "confidence": "high",
            }
        ],
        limitations=[],
        next_steps=[],
    )

    class FakeResponses:
        async def parse(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                output_parsed=expected,
                usage=SimpleNamespace(input_tokens=77, output_tokens=18),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls["client_options"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeOpenAI)
    model = OpenAICompatibleModel(
        api_key="not-a-real-key",
        model="model-from-config",
        base_url="https://llm.example.test/v1",
        fallback_models=(),
        max_output_tokens=300,
        max_evidence_chars=1000,
        max_concurrent=2,
    )
    evidence = EvidenceRecord(
        id="M1-ROI-WEIGHTED-20260601-18",
        workspace_id="demo",
        dataset_version="v1",
        domain="live_roi",
        record_type="fact",
        content="ROI is weighted and sourced.",
        source_material="M1",
        source_file="m1.xlsx",
        source_sheet="Sheet1",
        source_field="ROI",
        confidence="high",
    )
    result, usage = await model.synthesize("ROI怎么算", [evidence])
    assert result == expected
    assert usage == {"input_tokens": 77, "output_tokens": 18, "model": "model-from-config"}
    assert calls["model"] == "model-from-config"
    assert calls["text_format"] is GroundedAnswer
    assert calls["store"] is False
    assert "M1-ROI-WEIGHTED-20260601-18" in calls["input"][1]["content"]


def test_feedback_is_workspace_scoped_and_tied_to_dataset_version():
    client, repo = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "仓库工单和补发订单比例是多少？"},
        )
        body = response.json()
        feedback = client.post(
            "/v1/workspaces/demo/feedback",
            headers={"X-API-Key": "test-token"},
            json={
                "run_id": body["run_id"],
                "thread_id": body["thread_id"],
                "dataset_version": body["dataset_version"],
                "decision": "needs_validation",
                "notes": "按材料口径继续核验。",
            },
        )
    assert response.status_code == 200
    assert feedback.status_code == 202
    assert repo.feedback[-1]["dataset_version"] == body["dataset_version"]


def test_ingestion_and_questions_reject_direct_identifiers():
    from pydantic import ValidationError

    from audience_agent.contracts import AnalyzeRequest, EvidenceRecord

    with pytest.raises(ValidationError):
        AnalyzeRequest(question="请查一下用户手机号13812345678为什么退款")
    with pytest.raises(ValidationError):
        EvidenceRecord(
            id="unsafe-record",
            workspace_id="demo",
            dataset_version="v1",
            domain="experience",
            record_type="quote",
            content="用户联系13812345678投诉",
            source_material="M3",
            source_file="fake.xlsx",
            source_sheet="评论",
            source_field="text",
            confidence="low",
        )


def test_evidence_ingestion_is_staged_and_does_not_replace_live_version():
    from audience_agent.seeding import load_seed

    settings = make_settings()
    client, repo = make_client()
    with client:
        # Ask once to reveal the active snapshot version from the API contract.
        insight = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "直播投放ROI如何按实际开播日期汇总？"},
        ).json()
        active_version = insight["dataset_version"]
        record = load_seed(settings.seed_path)[0].model_dump()
        record.update(
            {
                "id": "M1-STAGED-NEW",
                "dataset_version": "next-approved-snapshot",
                "content": "staged replacement record for a controlled test",
            }
        )
        response = client.post(
            "/v1/workspaces/demo/evidence",
            headers={"X-API-Key": "test-token"},
            json=[record],
        )
    assert response.status_code == 202
    assert response.json()["status"] == "staged"
    assert asyncio.run(repo.active_dataset_version("demo")) == active_version


def test_request_body_limit_rejects_oversized_payload():
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token", "Content-Type": "application/json"},
            content=b"x" * 2_000_001,
        )
    assert response.status_code == 413


def test_validation_errors_do_not_echo_personal_identifiers():
    client, _ = make_client()
    with client:
        response = client.post(
            "/v1/workspaces/demo/insights",
            headers={"X-API-Key": "test-token"},
            json={"question": "请查一下手机号13812345678的退款问题"},
        )
    assert response.status_code == 422
    assert "13812345678" not in response.text


@pytest.mark.asyncio
async def test_output_claim_with_direct_identifier_is_removed():
    class PIIModel:
        async def synthesize(self, question, evidence, *, deadline=None):
            return GroundedAnswer.model_validate(
                {
                    "claims": [
                        {
                            "text": "用户电话13812345678，该手机号需要标记。",
                            "kind": "observation",
                            "evidence_ids": [evidence[0].id],
                            "confidence": "high",
                        }
                    ],
                    "limitations": [],
                    "next_steps": [],
                }
            ), {"input_tokens": 0, "output_tokens": 3, "model": "test"}

    settings = make_settings()
    repo = InMemoryEvidenceRepository(load_seed(settings.seed_path))
    graph = build_graph(repo, PIIModel(), settings).compile(
        checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    )
    result = await graph.ainvoke(
        {"workspace_id": "demo", "question": "面料和穿着体验反馈有哪些？", "run_id": "pii-test"},
        config={"configurable": {"thread_id": "demo:pii-thread"}},
    )
    assert result["status"] == "abstained"
    assert result["answer"]["claims"] == []
    assert any("直接个人标识符" in item for item in result["answer"]["limitations"])
