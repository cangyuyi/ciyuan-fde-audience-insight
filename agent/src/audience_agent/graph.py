from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from audience_agent.config import Settings
from audience_agent.contracts import GroundedAnswer
from audience_agent.llm import InsightModel
from audience_agent.privacy import contains_direct_identifier
from audience_agent.repository import EvidenceRepository


class InsightState(TypedDict, total=False):
    workspace_id: str
    question: str
    run_id: str
    deadline: float
    intent: str | None
    evidence_ids: list[str]
    evidence_materials: dict[str, str]
    dataset_version: str | None
    answer: dict[str, Any]
    token_usage: dict[str, int | str]
    status: str


INTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("live_roi", ("投放", "roi", "消耗", "千川", "自然日")),
    ("fulfillment", ("仓库", "工单", "补发", "履约", "发货异常")),
    ("audience_profile", ("年轻", "gen z", "genz", "画像", "人群", "漾 young", "18-30")),
    ("experience", ("面料", "闷", "卷边", "跑杯", "舒适", "体验", "评论", "客服", "显小")),
)
CROSS_DOMAINS = ("audience_profile", "experience", "fulfillment", "cross_domain")
MATERIAL_ROUTES = {
    "m1": "live_roi",
    "m2": "audience_profile",
    "m3": "experience",
    "m4": "fulfillment",
    "m5": "cross_domain",
    "m6": "cross_domain",
}
FOLLOWUP_PATTERNS = (
    "反例",
    "替代解释",
    "证据不足",
    "证据不够",
    "不足之处",
    "最小验证",
    "验证动作",
    "这些假设",
    "上述假设",
    "这些结论",
    "上述结论",
)


def classify_intent(question: str, previous_intent: str | None = None) -> str | None:
    normalized = re.sub(r"\s+", " ", question.casefold())
    matched_domains = {
        domain for domain, keywords in INTENT_PATTERNS if any(keyword in normalized for keyword in keywords)
    }
    matched_domains.update(
        domain for material, domain in MATERIAL_ROUTES.items() if material in normalized
    )
    joining_language = any(term in normalized for term in ("联合", "综合", "结合"))
    cross_question = (
        any(term in normalized for term in ("跨域", "跨材料", "人群洞察", "人群假设"))
        or re.search(r"综合.*(?:人群|证据|洞察|m2|材料)", normalized) is not None
        or ("场景" in normalized and "任务" in normalized)
        or (joining_language and len(matched_domains) >= 2)
        or len(matched_domains) >= 3
        or ("漾young" in normalized and len(matched_domains) >= 2)
        or ("cross_domain" in matched_domains and len(matched_domains) >= 2)
    )
    if cross_question:
        return "cross_domain"
    for material, domain in MATERIAL_ROUTES.items():
        if material in normalized:
            return domain
    for domain, keywords in INTENT_PATTERNS:
        if any(keyword in normalized for keyword in keywords):
            return domain
    if any(term in normalized for term in FOLLOWUP_PATTERNS):
        # Meta-questions must not lose the evidence domain merely because they omit its keywords.
        if previous_intent in {"live_roi", "audience_profile", "experience", "fulfillment", "cross_domain"}:
            return previous_intent
        return "cross_domain"
    return None


def build_graph(
    repository: EvidenceRepository,
    model: InsightModel,
    settings: Settings,
):
    async def route_node(state: InsightState) -> dict[str, Any]:
        return {
            "intent": classify_intent(
                state["question"], previous_intent=state.get("intent")
            ),
            "status": "processing",
        }

    async def retrieve_node(state: InsightState) -> dict[str, Any]:
        intent = state.get("intent")
        if intent is None:
            return {"evidence_ids": [], "evidence_materials": {}, "dataset_version": None}
        version = await repository.active_dataset_version(state["workspace_id"])
        if version is None:
            return {"evidence_ids": [], "evidence_materials": {}, "dataset_version": None}
        if intent == "cross_domain":
            records = await repository.search_domains(
                workspace_id=state["workspace_id"],
                dataset_version=version,
                domains=CROSS_DOMAINS,
                question=state["question"],
                limit=settings.max_evidence_items,
            )
        else:
            records = await repository.search(
                workspace_id=state["workspace_id"],
                dataset_version=version,
                domain=intent,
                question=state["question"],
                limit=settings.max_evidence_items,
            )
        return {
            "evidence_ids": [record.id for record in records],
            "evidence_materials": {record.id: record.source_material for record in records},
            "dataset_version": version,
        }

    async def synthesize_node(state: InsightState) -> dict[str, Any]:
        version = state.get("dataset_version")
        records = (
            await repository.get_many(state["workspace_id"], version, state.get("evidence_ids", []))
            if version
            else []
        )
        if not records:
            return {
                "answer": GroundedAnswer(
                    claims=[],
                    limitations=["当前授权材料没有找到足以回答该问题的证据。"],
                    next_steps=["补充带来源、时间范围和授权说明的数据，再重新分析。"],
                ).model_dump(),
                "token_usage": {"input_tokens": 0, "output_tokens": 0},
            }
        answer, usage = await model.synthesize(
            state["question"], records, deadline=state.get("deadline")
        )
        return {"answer": answer.model_dump(), "token_usage": usage}

    async def verify_node(state: InsightState) -> dict[str, Any]:
        answer = GroundedAnswer.model_validate(state.get("answer") or {})
        allowed_ids = set(state.get("evidence_ids", []))
        materials = state.get("evidence_materials", {})
        valid_claims = []
        dropped = 0
        pii_dropped = 0
        opinion_dropped = 0
        for claim in answer.claims:
            citations = list(
                dict.fromkeys(
                    source_id for source_id in claim.evidence_ids if source_id in allowed_ids
                )
            )
            if not citations:
                dropped += 1
                continue
            if contains_direct_identifier(claim.text):
                pii_dropped += 1
                continue
            cited_materials = {materials.get(source_id, "") for source_id in citations}
            if "M5" in cited_materials and (
                claim.kind == "observation"
                or not any(term in claim.text for term in ("观点", "假设", "待验证"))
            ):
                opinion_dropped += 1
                continue
            claim.evidence_ids = citations
            valid_claims.append(claim)

        limitations: list[str] = []

        def append_limitation(text: str) -> None:
            if (
                len(limitations) < 6
                and text
                and text not in limitations
                and not contains_direct_identifier(text)
            ):
                limitations.append(text)

        # Mandatory boundaries outrank model prose. Five slots cover every simultaneous safety
        # event while leaving one slot for a model limitation; none can be silently truncated.
        if dropped:
            append_limitation(f"系统已丢弃{dropped}条未绑定本次检索证据的主张。")
        if pii_dropped:
            append_limitation(f"系统已丢弃{pii_dropped}条包含直接个人标识符的主张。")
        if opinion_dropped:
            append_limitation(f"系统已丢弃{opinion_dropped}条把M5观点误述为事实的主张。")
        if state.get("intent") == "cross_domain":
            append_limitation("跨域证据口径和窗口不同，不将单域指标外推为整体人群或漾 YOUNG结论。")
        if "M5" in materials.values():
            append_limitation("M5仅为观点或待验证假设，不得作为已验证事实或单独的人群结论。")
        for text in answer.limitations:
            append_limitation(text)
        safe_next_steps = list(
            dict.fromkeys(
                text for text in answer.next_steps if not contains_direct_identifier(text)
            )
        )[:5]
        if not valid_claims and not limitations:
            append_limitation("没有可验证来源的主张，系统选择不作结论。")
        final = GroundedAnswer(
            claims=valid_claims,
            limitations=limitations,
            next_steps=safe_next_steps,
        )
        status = "answered" if valid_claims else "abstained"
        return {"answer": final.model_dump(), "status": status}

    def unsupported_node(state: InsightState) -> dict[str, Any]:
        answer = GroundedAnswer(
            claims=[],
            limitations=["问题超出当前接入的M1–M6授权证据范围；为避免猜测，本次不作结论。"],
            next_steps=[
                "可询问投放ROI、人群画像、穿着体验、履约或跨域人群洞察；其他主题需先接入并审核来源。"
            ],
        )
        return {
            "answer": answer.model_dump(),
            "status": "abstained",
            "evidence_ids": [],
            "evidence_materials": {},
            "dataset_version": None,
            "token_usage": {},
        }

    def after_route(state: InsightState) -> str:
        return "unsupported" if state.get("intent") is None else "retrieve"

    builder = StateGraph(InsightState)
    builder.add_node("route", route_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("verify_citations", verify_node)
    builder.add_node("abstain_unsupported", unsupported_node)
    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route", after_route, {"unsupported": "abstain_unsupported", "retrieve": "retrieve"}
    )
    builder.add_edge("retrieve", "synthesize")
    builder.add_edge("synthesize", "verify_citations")
    builder.add_edge("verify_citations", END)
    builder.add_edge("abstain_unsupported", END)
    return builder
