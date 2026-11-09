from __future__ import annotations

import json
from pathlib import Path

from audience_agent.contracts import EvidenceRecord

SEED_VERSION = "challenge-v1.0-derived-2026-11-04"


def build_seed_records(payload: dict, workspace_id: str = "demo") -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []

    def add(
        record_id: str,
        domain: str,
        record_type: str,
        content: str,
        material: str,
        source_file: str,
        sheet: str,
        field: str,
        row: str | None,
        confidence: str,
        metadata: dict | None = None,
    ) -> None:
        records.append(
            EvidenceRecord(
                id=record_id,
                workspace_id=workspace_id,
                dataset_version=SEED_VERSION,
                domain=domain,
                record_type=record_type,
                content=content,
                source_material=material,
                source_file=source_file,
                source_sheet=sheet,
                source_field=field,
                source_row=row,
                confidence=confidence,
                metadata=metadata or {},
            )
        )

    roi = payload["m1_session_roi"]
    add(
        "M1-ROI-WEIGHTED-20260601-18",
        "live_roi",
        "fact",
        f"按 M1-02 真实直播开始日期匹配 M1-01；{roi['session_dates']}个直播日匹配到{roi['joined_natural_day_rows']}条自然日投放记录。以演示门槛≥{roi['threshold_yuan']:.0f}元排除低额残余后，{roi['eligible_rows']}行总消耗{roi['total_spend_yuan']:.2f}元，整体成交金额{roi['total_overall_gmv_yuan']:.2f}元，加权整体ROI {roi['weighted_overall_roi']:.4f}；报告日ROI简单均值{roi['unweighted_reported_daily_roi_mean']:.4f}。",
        "M1",
        "M1-01_巨量千川直播投放_2026-06-01至2026-06-18_品牌A_脱敏版.xlsx + M1-02_抖音直播明细_2026-06-01至2026-06-18_账号A_脱敏版.xlsx",
        "Sheet1 + 直播间明细",
        "日期/整体消耗/整体成交金额/整体支付ROI + 直播开始时间",
        "2026-06-02至2026-06-17",
        "high",
        {
            "formula": "sum(整体成交金额) / sum(整体消耗)",
            "demo_threshold_yuan": roi["threshold_yuan"],
            "not_platform_standard": True,
        },
    )
    for excluded in roi["excluded_rows"]:
        add(
            f"M1-ROI-RESIDUAL-{excluded['date']}",
            "live_roi",
            "caveat",
            f"自然日{excluded['date']}记录消耗{excluded['spend']:.2f}元、整体成交金额{excluded['gmv']:.2f}元、平台整体支付ROI{excluded['reported_roi']:.0f}。该极小额跨日残余是真实原始记录，但不宜当作独立直播场次；演示中不纳入≥{roi['threshold_yuan']:.0f}元的独立日汇总。阈值需业务确认。",
            "M1",
            "M1-01_巨量千川直播投放_2026-06-01至2026-06-18_品牌A_脱敏版.xlsx",
            "Sheet1",
            "日期/整体消耗/整体成交金额/整体支付ROI",
            excluded["date"],
            "high",
            {"raw_record_preserved": True, "screen_threshold_yuan": roi["threshold_yuan"]},
        )

    m2 = payload["m2_genz"]
    add(
        "M2-GENZ-24-SESSION-MEAN",
        "audience_profile",
        "fact",
        "品牌A 24场直播 Gen Z 标签占比场次等权均值：曝光{:.2f}%，互动{:.2f}%，特定成交条件组{:.2f}%。此为每场画像占比的等权平均，不是人数加权总体比例。".format(
            m2["exposure"]["mean"] * 100,
            m2["interaction"]["mean"] * 100,
            m2["qualified"]["mean"] * 100,
        ),
        "M2",
        "M2-01/02/03_抖音人群数据_24场",
        "基本属性",
        "8大人群分布/genz",
        "24个直播日文件",
        "high",
        {
            "non_nested": True,
            "not_young_brand_data": True,
            "aggregation": "unweighted_session_mean",
        },
    )
    add(
        "M2-NON-NESTED-CAVEAT",
        "audience_profile",
        "caveat",
        "M2曝光、互动、特定成交条件组不是严格嵌套。特定成交条件组还叠加粉丝、互动、首购、观看时长等筛选；不得相除为曝光→互动→成交转化漏斗，也不能把品牌A画像当作漾 YOUNG 用户画像。",
        "M6",
        "M6-01_隐藏_数据口径抽样与来源说明_脱敏版.xlsx",
        "M2人群口径",
        "数据组/实际筛选含义/禁止用法",
        None,
        "high",
        {"candidate_visible_excerpt": True},
    )

    for row in payload.get("m3_quality_summary", []):
        if row["model"] not in {"型号K", "型号M"}:
            continue
        add(
            f"M3-{row['model']}-{row['issue']}",
            "experience",
            "fact",
            f"M3-01人工月报产品质量分析归类：{row['model']}—{row['issue']}，{row['count']}，占比{row['share']}。此为月报归纳样本，不代表全量问题发生率。",
            "M3",
            "M3-01_产品运营月报_2026-06_品牌A_脱敏版.xlsx",
            "产品质量分析",
            "商品/问题/次数/占比",
            row["model"],
            "medium",
            {"manual_summary": True, "not_population_rate": True},
        )
    for i, item in enumerate(payload.get("m3_quotes", [])):
        add(
            f"M3-QUOTE-{i + 1:02d}",
            "experience",
            "quote",
            f"M3-01脱敏评价原声（问题分类：{item['issue']}）：{item['quote']}",
            "M3",
            "M3-01_产品运营月报_2026-06_品牌A_脱敏版.xlsx",
            "汇总",
            "评价内容/问题描述",
            f"脱敏样本-{i + 1}",
            "medium",
            {"period": "2026-06", "sample_not_full_chat": True},
        )
    add(
        "M3-JULY-SAMPLE-CAVEAT",
        "experience",
        "caveat",
        "M3-02 客户之声是2026-07-22至2026-07-26的品牌A近期人工样本，不是6月全量客服对话，也不是漾 YOUNG 用户样本；只能提供待验证问题线索。",
        "M3",
        "M3-02_客户之声_2026-07-22至2026-07-26_品牌A_脱敏版.xlsx",
        "客户之声明细",
        "登记日期/类型/二级标签/客户原声",
        "2026-07-22至2026-07-26",
        "high",
        {"period": "2026-07-22/26", "not_young_brand_data": True},
    )

    m4 = payload["m4_authoritative"]
    add(
        "M4-WAREHOUSE-ORDER-RATE",
        "fulfillment",
        "fact",
        f"M4-03A/M6同批口径：{m4['paid_orders']:,}笔支付去重主订单中，{m4['warehouse_issue_orders']:,}笔关联仓库问题工单（{m4['issue_rate'] * 100:.3f}%）；{m4['resend_orders']:,}笔关联补发（{m4['resend_rate'] * 100:.3f}%）。工单关联不证明退款责任归属。",
        "M4",
        "M4-03A_全量订单分母与关联说明_2026-06-01至2026-06-18_品牌A_脱敏版.xlsx",
        "总体汇总",
        "支付订单/命中仓库问题工单的支付订单/命中补发订单的支付订单",
        "总体",
        "high",
        {"denominator": "deduplicated paid parent orders", "not_causation": True},
    )
    add(
        "M4-GRAIN-CAVEAT",
        "fulfillment",
        "caveat",
        "M4全量订单底表是商品明细粒度，商品行数不等于订单数；一张主订单可有多商品行。问题工单、补发与退款是不同流程，计算订单率前须按稳定ORD键去重；不可把全部退款归因仓库。",
        "M6",
        "M6-01_隐藏_数据口径抽样与来源说明_脱敏版.xlsx",
        "M4履约数据口径",
        "实际样本/正确用法/禁止用法",
        None,
        "high",
        {"candidate_visible_excerpt": True},
    )
    return records


def load_seed(path: Path, workspace_id: str = "demo") -> list[EvidenceRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") == "evidence.raw.v1":
        return [
            EvidenceRecord.model_validate({**item, "workspace_id": workspace_id})
            for item in payload["records"]
        ]
    # Legacy derived fixture remains readable for backwards-compatible regression tests.
    return build_seed_records(payload, workspace_id=workspace_id)
