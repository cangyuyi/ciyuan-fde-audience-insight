from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import load_workbook

from audience_agent.contracts import EvidenceRecord

RAW_SCHEMA_VERSION = "evidence.raw.v1"
MANIFEST_SCHEMA_VERSION = "raw-manifest.v1"
EXPECTED_FILES = {"M1": 5, "M2": 72, "M3": 2, "M4": 6, "M5": 1, "M6": 1}
TIME_WINDOWS = {
    "M1": "M1-03: 2026-05; M1-04: 2026-06; M1-05: 2026-05+2026-06; 直播ROI: 2026-06-02至06-17",
    "M2": "2026-06-02至2026-06-30，24场/23个自然日（06-19两场）",
    "M3": "M3-01: 2026-06人工月报；M3-02: 2026-07-22至07-26人工样本",
    "M4": "订单提交2026-06-01至06-18；问题暴露观察截至2026-06，存在右删失",
    "M5": "访谈整理摘要版本2026-07，仅opinion/hypothesis",
    "M6": "口径与权限说明，覆盖M1–M6各自窗口",
}
CLEANING_RULES = {
    "M1": [
        "千川金额字段去除千分位后转Decimal；直播开始日期取真实场次日期并关联自然日投放行。",
        "演示ROI仅纳入匹配直播日且整体消耗>=100元的行；原始低额残余另存caveat，不删除。",
        "加权ROI=sum(整体成交金额)/sum(整体消耗)，同时保留平台日ROI简单均值。",
        "M1-05保留5月和6月两个完整自然月，不并入单一2026-06窗口。",
    ],
    "M2": [
        "逐份读取基本属性!A5:B5的genz占比；三组各24份，缺组或场次不匹配即失败。",
        "场次按文件名日期+起止时间识别；自然日单独计数，不能把24场写成24个日期。",
        "三组只做画像占比的场次等权统计，不相除构造非嵌套漏斗，不外推长期趋势。",
    ],
    "M3": [
        "产品质量分析按型号所在真实行读取次数与占比，数值型占比统一格式化为百分比。",
        "汇总!L列与客户之声!K列原声逐字保留（含换行），不截断；空原声不生成quote。",
        "6月人工月报与7月人工样本分开标记；均不得解释为6月全量聊天或总体问题率。",
    ],
    "M4": [
        "三批商品行按主订单编号去重；支付分母要求支付完成时间非空。",
        "支付订单金额每个主订单只计首次金额；另保留支付商品行直接求和作为重复金额警示。",
        "工单使用商品信息_订单编号；补发合并线上订单号与原始线上订单号后关联支付主订单。",
        "只输出聚合指标，不输出ORD/USER等记录级键；空仓库字段不等于无问题，比例不外推终态。",
    ],
    "M5": [
        "python-docx直接读取主题/观点摘要表及真实表号、行号；不是逐字稿。",
        "所有M5记录强制为opinion或hypothesis、low confidence，禁止fact/quote/caveat。",
        "年轻人群内容只能形成待验证假设，不作为确定Persona、市场规模或付费意愿证据。",
    ],
    "M6": [
        "按真实sheet和行号导入非嵌套、采样、订单粒度、右删失及权限边界caveat。",
        "保留真实文件名末尾(1)，不构造合并文件名，不绕过权限或补造缺失数据。",
    ],
}


@dataclass
class RawImportResult:
    evidence: dict[str, Any]
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: Any) -> Decimal:
    if value in (None, "", "-"):
        return Decimal("0")
    return Decimal(str(value).replace(",", "").replace("%", ""))


def _one(root: Path, prefix: str) -> Path:
    matches = sorted(path for path in root.rglob(f"{prefix}*.xlsx") if path.is_file())
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {prefix} XLSX, found {len(matches)}")
    return matches[0]


def _dict_rows(path: Path, sheet_name: str) -> list[tuple[int, dict[str, Any]]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[sheet_name]
        iterator = sheet.iter_rows(values_only=True)
        headers = [str(value) if value is not None else "" for value in next(iterator)]
        rows = []
        for row_number, values in enumerate(iterator, start=2):
            if any(value is not None for value in values):
                rows.append((row_number, dict(zip(headers, values, strict=False))))
        return rows
    finally:
        workbook.close()


def _record(
    record_id: str,
    dataset_version: str,
    domain: str,
    record_type: str,
    content: str,
    material: str,
    source_file: Path | str,
    sheet: str,
    field: str,
    row: str | None,
    confidence: str,
    metadata: dict[str, Any] | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        id=record_id,
        workspace_id="demo",
        dataset_version=dataset_version,
        domain=domain,
        record_type=record_type,
        content=content,
        source_material=material,
        source_file=source_file.name if isinstance(source_file, Path) else source_file,
        source_sheet=sheet,
        source_field=field,
        source_row=row,
        confidence=confidence,
        metadata=metadata or {},
    )


def _import_m1(root: Path, version: str) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    ads_path = _one(root, "M1-01")
    live_path = _one(root, "M1-02")
    live_rows = _dict_rows(live_path, "直播间明细")
    session_dates: dict[str, int] = {}
    for row_number, row in live_rows:
        raw_date = str(row["直播开始时间"])[:10].replace("/", "-")
        session_dates[raw_date] = row_number
    ads_rows = _dict_rows(ads_path, "Sheet1")
    joined = []
    for row_number, row in ads_rows:
        date = str(row["日期"])[:10]
        if date in session_dates:
            joined.append(
                {
                    "date": date,
                    "spend": _decimal(row["整体消耗"]),
                    "gmv": _decimal(row["整体成交金额"]),
                    "reported_roi": _decimal(row["整体支付ROI"]),
                    "row": row_number,
                }
            )
    eligible = [row for row in joined if row["spend"] >= Decimal("100")]
    excluded = [row for row in joined if row["spend"] < Decimal("100")]
    if not eligible:
        raise ValueError("M1 has no eligible ROI rows")
    total_spend = sum((row["spend"] for row in eligible), Decimal("0"))
    total_gmv = sum((row["gmv"] for row in eligible), Decimal("0"))
    weighted = total_gmv / total_spend
    daily_mean = sum((row["reported_roi"] for row in eligible), Decimal("0")) / len(eligible)
    metrics = {
        "session_dates": len(session_dates),
        "joined_natural_day_rows": len(joined),
        "eligible_rows": len(eligible),
        "threshold_yuan": 100.0,
        "total_spend_yuan": float(total_spend),
        "total_overall_gmv_yuan": float(total_gmv),
        "weighted_overall_roi": float(weighted),
        "unweighted_reported_daily_roi_mean": float(daily_mean),
        "dates": [
            {
                "date": row["date"],
                "spend": float(row["spend"]),
                "gmv": float(row["gmv"]),
                "reported_roi": float(row["reported_roi"]),
                "source_row": row["row"],
            }
            for row in sorted(eligible, key=lambda item: item["date"])
        ],
        "excluded_rows": [
            {
                "date": row["date"],
                "spend": float(row["spend"]),
                "gmv": float(row["gmv"]),
                "reported_roi": float(row["reported_roi"]),
                "source_row": row["row"],
            }
            for row in excluded
        ],
        "live_sheet_rows": session_dates,
    }
    source_files = f"{ads_path.name} + {live_path.name}"
    records = [
        _record(
            "M1-ROI-WEIGHTED-20260601-18",
            version,
            "live_roi",
            "fact",
            f"按M1-02真实直播开始日期匹配M1-01；{len(session_dates)}个直播日匹配{len(joined)}条自然日投放记录。以演示门槛≥100元排除低额残余后，{len(eligible)}行总消耗{total_spend:.2f}元，整体成交金额{total_gmv:.2f}元，加权整体ROI {weighted:.4f}；报告日ROI简单均值{daily_mean:.4f}。门槛不是平台标准。",
            "M1",
            source_files,
            "Sheet1 + 直播间明细",
            "日期/整体消耗/整体成交金额/整体支付ROI + 直播开始时间",
            f"M1-01 rows {','.join(str(row['row']) for row in eligible)}; M1-02 rows {','.join(str(row) for row in session_dates.values())}; 2026-06-02至06-17",
            "high",
            {
                "formula": "sum(整体成交金额)/sum(整体消耗)",
                "demo_threshold_yuan": 100.0,
                "not_platform_standard": True,
                "time_window": "2026-06-02/2026-06-17",
                "source_rows": {row["date"]: row["row"] for row in joined},
            },
        )
    ]
    for row in excluded:
        records.append(
            _record(
                f"M1-ROI-RESIDUAL-{row['date']}",
                version,
                "live_roi",
                "caveat",
                f"自然日{row['date']}记录消耗{row['spend']:.2f}元、整体成交金额{row['gmv']:.2f}元、平台整体支付ROI {row['reported_roi']:.0f}。该极小额跨日残余是真实原始记录，但不宜当作独立直播场；演示中不纳入≥100元汇总。",
                "M1",
                ads_path,
                "Sheet1",
                "日期/整体消耗/整体成交金额/整体支付ROI",
                f"Sheet1!{row['row']}; {row['date']}",
                "high",
                {"raw_record_preserved": True, "screen_threshold_yuan": 100.0},
            )
        )
    refund_path = _one(root, "M1-05")
    workbook = load_workbook(refund_path, read_only=True, data_only=True)
    try:
        monthly = list(workbook["月度汇总"].iter_rows(min_row=5, max_row=6, values_only=True))
    finally:
        workbook.close()
    months = [str(row[0]) for row in monthly]
    days = sum(int(row[1]) for row in monthly)
    records.append(
        _record(
            "M1-REFUND-TWO-MONTH-WINDOW",
            version,
            "live_roi",
            "caveat",
            f"M1-05覆盖{'、'.join(months)}共{days}个自然日；退款费率按总分子/总分母加权，销售成本全为0，不能据此证明真实利润或把全部记录写成单一2026-06窗口。",
            "M1",
            refund_path,
            "月度汇总",
            "月份/自然日数/退款数量率/退款金额率/利润可用性",
            "月度汇总!A5:P6",
            "high",
            {"months": months, "natural_days": days, "profit_available": False},
        )
    )
    return records, metrics


def _session_key(path: Path) -> str:
    match = re.search(r"(2026-\d{2}-\d{2}_\d{2}-\d{2}至\d{2}-\d{2})", path.name)
    if not match:
        raise ValueError(f"cannot parse M2 session from {path.name}")
    return match.group(1)


def _import_m2(root: Path, version: str) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    groups = {"exposure": "M2-01", "interaction": "M2-02", "qualified": "M2-03"}
    metrics: dict[str, Any] = {}
    session_sets = []
    all_files: list[Path] = []
    for name, prefix in groups.items():
        paths = sorted(root.rglob(f"{prefix}*.xlsx"))
        if len(paths) != 24:
            raise ValueError(f"{prefix} must contain 24 session files, found {len(paths)}")
        session_sets.append({_session_key(path) for path in paths})
        values = []
        age_18_30_values = []
        for path in paths:
            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                sheet = workbook["基本属性"]
                if str(sheet["A5"].value).casefold() != "genz":
                    raise ValueError(f"genz not at 基本属性!A5 in {path.name}")
                values.append(float(sheet["B5"].value))
                if sheet["A16"].value != "18-24岁" or sheet["A17"].value != "25-30岁":
                    raise ValueError(f"age bands not at 基本属性!A16:A17 in {path.name}")
                age_18_30_values.append(float(sheet["B16"].value) + float(sheet["B17"].value))
            finally:
                workbook.close()
        metrics[name] = {
            "n": len(values),
            "mean": statistics.mean(values),
            "median": statistics.median(values),
            "min": min(values),
            "max": max(values),
            "source_cell": "基本属性!B5",
            "age_18_30": {
                "mean": statistics.mean(age_18_30_values),
                "median": statistics.median(age_18_30_values),
                "min": min(age_18_30_values),
                "max": max(age_18_30_values),
                "source_cells": "基本属性!B16:B17",
            },
        }
        all_files.extend(paths)
    if len(set(session_sets[0])) != 24 or any(keys != session_sets[0] for keys in session_sets[1:]):
        raise ValueError("M2 groups do not contain the same 24 sessions")
    dates = sorted({key[:10] for key in session_sets[0]})
    if len(dates) != 23:
        raise ValueError(f"M2 must span 23 distinct dates, found {len(dates)}")
    source_files = "\n".join(path.name for path in sorted(all_files, key=lambda item: item.name))
    content = (
        "品牌A 24场直播（23个自然日，2026-06-19两场）Gen Z标签占比场次等权均值："
        f"曝光{metrics['exposure']['mean']:.2%}，互动{metrics['interaction']['mean']:.2%}，"
        f"特定成交条件组{metrics['qualified']['mean']:.2%}。18–30岁（B16+B17）场次等权均值分别为"
        f"{metrics['exposure']['age_18_30']['mean']:.4%}/{metrics['interaction']['age_18_30']['mean']:.4%}/{metrics['qualified']['age_18_30']['mean']:.4%}。"
        "这些不是人数加权总体比例；窗口2026-06-02至06-30，单月不能证明长期趋势。"
    )
    record = _record(
        "M2-GENZ-24-SESSION-MEAN",
        version,
        "audience_profile",
        "fact",
        content,
        "M2",
        source_files,
        "基本属性",
        "8大人群分布/genz（A5:B5）+年龄18-24/25-30（A16:B17）",
        "24场/23个自然日；2026-06-02至2026-06-30；06-19两场",
        "high",
        {
            "session_count": 24,
            "date_count": 23,
            "duplicate_dates": {"2026-06-19": 2},
            "non_nested": True,
            "not_young_brand_data": True,
            "aggregation": "unweighted_session_mean",
            "single_month_not_long_term_trend": True,
            "statistics": metrics,
        },
    )
    return [record], metrics


def _percent(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.0%}"
    return str(value)


def _import_m3(root: Path, version: str) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    monthly_path = _one(root, "M3-01")
    july_path = _one(root, "M3-02")
    records: list[EvidenceRecord] = []
    quality = []
    workbook = load_workbook(monthly_path, read_only=True, data_only=True)
    try:
        sheet = workbook["产品质量分析"]
        for row_number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            model = values[0] if values else None
            if not (isinstance(model, str) and model.startswith("型号")):
                continue
            issue = values[1] if len(values) > 1 else None
            count = values[2] if len(values) > 2 else None
            share = values[3] if len(values) > 3 else None
            if not issue or not count:
                continue
            quality.append(
                {
                    "model": model,
                    "issue": issue,
                    "count": count,
                    "share": share,
                    "source_row": row_number,
                }
            )
            records.append(
                _record(
                    f"M3-{model}-{issue}-R{row_number}",
                    version,
                    "experience",
                    "fact",
                    f"M3-01人工月报产品质量分析：{model}—{issue}，{count}，占比{_percent(share)}。此为人工归纳样本，不代表全量问题发生率。",
                    "M3",
                    monthly_path,
                    "产品质量分析",
                    "商品/问题/次数/占比",
                    f"产品质量分析!A{row_number}:E{row_number}",
                    "medium",
                    {"manual_summary": True, "not_population_rate": True, "period": "2026-06"},
                )
            )
        summary = workbook["汇总"]
        quote_count = 0
        for row_number, values in enumerate(summary.iter_rows(min_row=2, values_only=True), start=2):
            quote = values[11] if len(values) > 11 else None
            if not isinstance(quote, str) or not quote:
                continue
            quote_count += 1
            issue = (values[5] if len(values) > 5 else None) or "未分类"
            records.append(
                _record(
                    f"M3-QUOTE-ROW-{row_number:05d}",
                    version,
                    "experience",
                    "quote",
                    f"M3-01脱敏评价原声（问题分类：{issue}）：{quote}",
                    "M3",
                    monthly_path,
                    "汇总",
                    "评价内容(L)/问题描述(F)",
                    f"汇总!L{row_number}",
                    "medium",
                    {
                        "period": "2026-06",
                        "sample_not_full_chat": True,
                        "source_cell": f"L{row_number}",
                        "verbatim_quote": quote,
                    },
                )
            )
    finally:
        workbook.close()
    july_rows = _dict_rows(july_path, "客户之声明细")
    july_count = 0
    july_topics: Counter[str] = Counter()
    for row_number, row in july_rows:
        # Topic distribution uses all 100 source rows, including a row with empty customer text.
        tag = row.get("二级标签") or "未分类"
        july_topics[str(tag)] += 1
        quote = row.get("客户原声")
        if not isinstance(quote, str) or not quote:
            continue
        july_count += 1
        records.append(
            _record(
                f"M3-JULY-QUOTE-ROW-{row_number:05d}",
                version,
                "experience",
                "quote",
                f"M3-02近期人工样本（{row.get('类型')}/{tag}）：{quote}",
                "M3",
                july_path,
                "客户之声明细",
                "登记日期/类型/二级标签/三级备注/客户原声(K)",
                f"客户之声明细!K{row_number}; 2026-07-22至07-26",
                "medium",
                {
                    "period": "2026-07-22/2026-07-26",
                    "sample_not_full_chat": True,
                    "not_young_brand_data": True,
                    "verbatim_quote": quote,
                },
            )
        )
    records.append(
        _record(
            "M3-JULY-TOPIC-COUNTS",
            version,
            "experience",
            "fact",
            "M3-02近期人工样本二级标签计数："
            + "、".join(f"{topic}{count}条" for topic, count in july_topics.most_common())
            + f"。这是{len(july_rows)}条人工样本的标签分布（含空原声行），非总体问题率；产品功能、尺码与舒适体验可用于形成待验证场景线索。",
            "M3",
            july_path,
            "客户之声明细",
            "二级标签(I)/客户原声(K)",
            f"客户之声明细!I2:K{len(july_rows) + 1}; 2026-07-22至07-26",
            "medium",
            {
                "topic_counts": dict(july_topics),
                "sample_not_full_chat": True,
                "record_count": len(july_rows),
                "nonempty_quote_count": july_count,
            },
        )
    )
    records.append(
        _record(
            "M3-JULY-SAMPLE-CAVEAT",
            version,
            "experience",
            "caveat",
            f"M3-02客户之声共读取{july_count}条非空原声，窗口仅为2026-07-22至07-26的品牌A人工样本；不是6月全量客服对话，也不是漾 YOUNG用户样本，只能提供待验证线索。",
            "M3",
            july_path,
            "客户之声明细",
            "登记日期/类型/二级标签/客户原声",
            f"客户之声明细!2:{len(july_rows) + 1}; 2026-07-22至07-26",
            "high",
            {"period": "2026-07-22/26", "record_count": july_count},
        )
    )
    return records, {
        "quality_summary": quality,
        "june_verbatim_quotes": quote_count,
        "july_rows": len(july_rows),
        "july_verbatim_quotes": july_count,
        "july_topic_counts": dict(july_topics),
        "full_l13_verified": any(
            item.metadata.get("source_cell") == "L13"
            and "多穿一会儿做些大动作就能都发现了" in item.metadata.get("verbatim_quote", "")
            for item in records
        ),
    }


def _import_m4(root: Path, version: str) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    issue_path = _one(root, "M4-01")
    resend_path = _one(root, "M4-02")
    summary_path = _one(root, "M4-03A")
    issue_rows = _dict_rows(issue_path, "仓库问题工单")
    issue_keys = {
        str(row["商品信息_订单编号(客服登记)"])
        for _, row in issue_rows
        if row.get("商品信息_订单编号(客服登记)")
    }
    resend_keys: set[str] = set()
    for _, row in _dict_rows(resend_path, "补发订单明细"):
        for field in ("线上订单号", "原始线上订单号"):
            if row.get(field):
                resend_keys.add(str(row[field]))
    detail_paths = sorted(root.rglob("M4-03[BCD]*.xlsx"))
    if len(detail_paths) != 3:
        raise ValueError(f"expected three M4 detail batches, found {len(detail_paths)}")
    raw_rows = 0
    all_orders: set[str] = set()
    paid_orders: set[str] = set()
    issue_paid: set[str] = set()
    resend_paid: set[str] = set()
    paid_amount = Decimal("0")
    paid_row_amount = Decimal("0")
    detail_row_ranges = []
    for path in detail_paths:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            iterator = sheet.iter_rows(values_only=True)
            headers = list(next(iterator))
            indexes = {name: index for index, name in enumerate(headers)}
            batch_rows = 0
            for values in iterator:
                if not any(value is not None for value in values):
                    continue
                raw_rows += 1
                batch_rows += 1
                order_id = str(values[indexes["主订单编号"]])
                all_orders.add(order_id)
                paid = values[indexes["支付完成时间"]] not in (None, "")
                if not paid:
                    continue
                amount = _decimal(values[indexes["订单应付金额"]])
                paid_row_amount += amount
                if order_id not in paid_orders:
                    paid_amount += amount
                paid_orders.add(order_id)
                if order_id in issue_keys:
                    issue_paid.add(order_id)
                if order_id in resend_keys:
                    resend_paid.add(order_id)
            detail_row_ranges.append(f"{path.name}:2-{batch_rows + 1}")
        finally:
            workbook.close()
    level1_orders: defaultdict[str, set[str]] = defaultdict(set)
    level2_orders: defaultdict[str, set[str]] = defaultdict(set)
    for _, row in issue_rows:
        order_id = str(row.get("商品信息_订单编号(客服登记)"))
        if order_id not in paid_orders:
            continue
        level1 = row.get("商品问题_层级1(客服登记)")
        level2 = row.get("商品问题_层级2(客服登记)")
        if level1:
            level1_orders[str(level1)].add(order_id)
        if level2:
            level2_orders[str(level2)].add(order_id)
    level1_counts = {label: len(orders) for label, orders in level1_orders.items()}
    level2_counts = {label: len(orders) for label, orders in level2_orders.items()}
    paid_count = len(paid_orders)
    issue_rate = len(issue_paid) / paid_count
    resend_rate = len(resend_paid) / paid_count
    metrics = {
        "product_rows": raw_rows,
        "parent_orders": len(all_orders),
        "paid_orders": paid_count,
        "warehouse_issue_orders": len(issue_paid),
        "resend_orders": len(resend_paid),
        "issue_rate": issue_rate,
        "resend_rate": resend_rate,
        "paid_parent_order_amount": float(paid_amount),
        "paid_product_row_amount": float(paid_row_amount),
        "window": "2026-06-01至2026-06-18",
        "resend_association_fields": ["线上订单号", "原始线上订单号"],
        "detail_row_ranges": detail_row_ranges,
        "issue_level1_paid_order_counts": level1_counts,
        "issue_level2_paid_order_counts": level2_counts,
        "labels_not_mutually_exclusive": True,
    }
    theme_summary = (
        f"卷边{level2_counts.get('J卷边', 0)}、跑杯{level2_counts.get('P跑杯', 0)}、"
        f"不显小{level2_counts.get('不显小', 0)}、承托力不足{level2_counts.get('C承托力不足', 0)}个支付主订单；"
        f"一级标签商品问题{level1_counts.get('商品问题', 0)}、尺码问题{level1_counts.get('尺码问题', 0)}个支付主订单。"
    )
    records = [
        _record(
            "M4-WAREHOUSE-ORDER-RATE",
            version,
            "fulfillment",
            "fact",
            f"M4三批商品明细复算：{raw_rows:,}商品行、{len(all_orders):,}去重主订单、{paid_count:,}支付主订单；{len(issue_paid):,}笔关联仓库问题工单（{issue_rate:.3%}），{len(resend_paid):,}笔关联补发（{resend_rate:.3%}）。支付去重金额{paid_amount:.2f}元；直接累加支付商品行金额{paid_row_amount:.2f}元会重复。关联不证明退款责任归属。",
            "M4",
            "\n".join(path.name for path in [summary_path, *detail_paths]),
            "总体汇总 + 订单明细_1/2/3",
            "主订单编号/支付完成时间/订单应付金额/是否命中仓库问题工单/是否命中补发订单",
            f"M4-03A总体汇总!A4:D15; {'; '.join(detail_row_ranges)}",
            "high",
            {"denominator": "deduplicated paid parent orders", "not_causation": True, **metrics},
        ),
        _record(
            "M4-ISSUE-THEME-PAID-ORDERS",
            version,
            "fulfillment",
            "fact",
            f"M4-01按稳定ORD键关联支付主订单的问题主题：{theme_summary}标签非互斥，不能相加为总人数或解释为人群占比；仅说明当前工单样本中的产品/尺码/体验线索。",
            "M4",
            issue_path,
            "仓库问题工单",
            "商品问题_层级1/层级2 + 商品信息_订单编号",
            f"仓库问题工单!P2:Z{len(issue_rows) + 1}; 2026-06；支付主订单去重",
            "high",
            {
                "level1_paid_order_counts": level1_counts,
                "level2_paid_order_counts": level2_counts,
                "labels_not_mutually_exclusive": True,
                "not_audience_population_rate": True,
            },
        ),
        _record(
            "M4-RIGHT-CENSORING-CAVEAT",
            version,
            "fulfillment",
            "caveat",
            "问题工单与补发材料截至2026年6月；临近2026-06-18的订单观察窗口较短，后续问题可能尚未充分暴露。当前关联率不能外推为成熟终态或长期稳定水平。",
            "M4",
            summary_path,
            "口径说明",
            "观察窗口/使用限制",
            "口径说明!A13:B16",
            "high",
            {"right_censored": True, "observation_end": "2026-06"},
        ),
    ]
    return records, metrics


def _import_m5(root: Path, version: str) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    path = next(root.rglob("M5-01*.docx"), None)
    if path is None:
        raise ValueError("M5 DOCX is missing")
    document = Document(path)
    records: list[EvidenceRecord] = []
    roles = {6: "经营负责人", 9: "产品负责人", 12: "项目/渠道负责人", 15: "内容策略负责人", 18: "达人合作负责人"}
    for table_number, table in enumerate(document.tables, start=1):
        if table_number not in roles:
            continue
        for row_number, row in enumerate(table.rows[1:], start=2):
            theme = row.cells[0].text.strip()
            opinion = row.cells[1].text.strip()
            if not opinion:
                continue
            records.append(
                _record(
                    f"M5-OPINION-T{table_number:02d}-R{row_number:02d}",
                    version,
                    "cross_domain",
                    "opinion",
                    f"{roles[table_number]}观点｜{theme}：{opinion}（访谈整理摘要，非客观事实，需交叉验证）",
                    "M5",
                    path,
                    f"DOCX table {table_number}",
                    "主题/观点摘要",
                    f"table={table_number},row={row_number}",
                    "low",
                    {"evidence_level": "opinion", "validation_required": True, "not_verbatim": True},
                )
            )
    young_paragraph = next(
        (
            (index, paragraph.text.strip())
            for index, paragraph in enumerate(document.paragraphs, start=1)
            if "团队曾通过跨部门共创" in paragraph.text
        ),
        None,
    )
    if young_paragraph is None:
        raise ValueError("M5 young-audience hypothesis paragraph is missing")
    paragraph_number, text = young_paragraph
    records.append(
        _record(
            "M5-HYPOTHESIS-YOUNG-VALIDATION",
            version,
            "cross_domain",
            "hypothesis",
            f"待验证假设：项目/渠道负责人认为需持续理解更年轻受众，但现有比较主要是内部假设。{text} 不得用品牌A或M2单域指标替代漾 YOUNG一手人群证据。",
            "M5",
            path,
            "DOCX paragraphs",
            "项目/渠道负责人访谈摘要",
            f"paragraph={paragraph_number}",
            "low",
            {"evidence_level": "hypothesis", "validation_required": True, "not_young_brand_data": True},
        )
    )
    return records, {"opinion_hypothesis_records": len(records), "version": "2026-07"}


def _import_m6(root: Path, version: str) -> list[EvidenceRecord]:
    path = _one(root, "M6-01")
    definitions = [
        (
            "M2-NON-NESTED-CAVEAT",
            "audience_profile",
            "M2人群口径",
            "三组关系/正确用法/禁止用法",
            "M2人群口径!A5:D6",
            "M2曝光、互动、特定成交条件组不是严格嵌套；不得相构造曝光→互动→成交漏斗。特定成交条件组不等于全部成交人群；品牌A画像不能当作漾 YOUNG画像。",
        ),
        (
            "M3-SAMPLING-WINDOW-CAVEAT",
            "experience",
            "M3近期样本口径",
            "6月聊天原文/补充样本/不可回答",
            "M3近期样本口径!A2:D3",
            "6月全量聊天原文不可得；7月22—26日样本只能作定性补充，不得冒充6月全量或估算总体问题率。",
        ),
        (
            "M4-GRAIN-AND-OBSERVATION-CAVEAT",
            "fulfillment",
            "M4履约数据口径",
            "订单粒度/关联率/观察与归属边界",
            "M4履约数据口径!A2:D7",
            "商品行不等于订单数；问题工单、补发与退款是不同流程。仓库字段为空不等于无问题；不得做仓库间比较、长期趋势或把全部退款归因仓库。",
        ),
        (
            "M5-OPINION-BOUNDARY-CAVEAT",
            "cross_domain",
            "M5专家访谈口径",
            "观点证据等级/材料缺口/禁止用法",
            "M5专家访谈口径!A2:D5",
            "M5是五类角色的整理摘要，不是逐字稿或企业共识；只能形成观点、冲突和待验证假设，不能单独证明Persona、人群规模、付费意愿或市场趋势。",
        ),
    ]
    return [
        _record(
            record_id,
            version,
            domain,
            "caveat",
            content,
            "M6",
            path,
            sheet,
            field,
            row,
            "high",
            {"candidate_visible_excerpt": True},
        )
        for record_id, domain, sheet, field, row, content in definitions
    ]


def _header_row(material: str, file_name: str, sheet_name: str) -> int:
    if file_name.startswith("M4-03A"):
        return {"口径说明": 3, "总体汇总": 3, "每日汇总": 3, "商品TOP100": 3, "分类汇总": 4, "字段处理说明": 3}.get(sheet_name, 1)
    if file_name.startswith("M1-05"):
        return {"口径说明": 3, "月度汇总": 4}.get(sheet_name, 1)
    if file_name.startswith("M3-01"):
        return {"产品质量分析": 9}.get(sheet_name, 1)
    if file_name.startswith(("M4-03B", "M4-03C", "M4-03D")):
        return {"使用说明": 3}.get(sheet_name, 1)
    return 1


def _xlsx_manifest(path: Path, material: str) -> list[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets = []
    try:
        for sheet in workbook.worksheets:
            header_row = _header_row(material, path.name, sheet.title)
            fields: list[str] = []
            last_nonempty = 0
            data_rows = 0
            for row_number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                if not any(value is not None for value in values):
                    continue
                last_nonempty = row_number
                if row_number == header_row:
                    fields = list(dict.fromkeys(str(value) for value in values if value is not None))
                elif row_number > header_row:
                    data_rows += 1
            sheets.append(
                {
                    "sheet": sheet.title,
                    "header_row": header_row,
                    "fields": fields,
                    "data_row_start": header_row + 1 if last_nonempty > header_row else None,
                    "last_nonempty_row": last_nonempty,
                    "nonempty_data_rows": data_rows,
                }
            )
    finally:
        workbook.close()
    return sheets


def _file_window(material: str, name: str) -> str:
    if material == "M2":
        return TIME_WINDOWS["M2"]
    if name.startswith("M1-03"):
        return "2026-05完整自然月"
    if name.startswith("M1-04"):
        return "2026-06完整自然月"
    if name.startswith("M1-05"):
        return "2026-05与2026-06，共61个自然日"
    if name.startswith("M1-01"):
        return "文件名任务窗2026-06-01至06-18；表内自然日记录2026-06-02至06-22"
    if name.startswith("M1-02"):
        return "真实开播日期2026-06-02至06-17，12个直播日"
    return TIME_WINDOWS[material]


def import_raw_data(raw_root: Path) -> RawImportResult:
    root = raw_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"raw data root is not a directory: {root}")
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".xlsx", ".docx"}),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    materials = {path.name[:2] for path in files}
    for material, expected in EXPECTED_FILES.items():
        actual = sum(path.name.startswith(material) for path in files)
        if actual != expected:
            raise ValueError(f"{material}: expected {expected} files, found {actual}")
    if materials != set(EXPECTED_FILES):
        raise ValueError(f"unexpected material directories/files: {sorted(materials)}")
    file_hashes = {path: _sha256(path) for path in files}
    version_input = "\n".join(
        f"{path.relative_to(root).as_posix()}:{file_hashes[path]}" for path in files
    )
    version = f"m1-m6-raw-{hashlib.sha256(version_input.encode()).hexdigest()[:12]}"

    m1_records, m1_metrics = _import_m1(root, version)
    m2_records, m2_metrics = _import_m2(root, version)
    m3_records, m3_metrics = _import_m3(root, version)
    m4_records, m4_metrics = _import_m4(root, version)
    m5_records, m5_metrics = _import_m5(root, version)
    m6_records = _import_m6(root, version)
    records = [*m1_records, *m2_records, *m3_records, *m4_records, *m5_records, *m6_records]
    identifiers = [record.id for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate evidence IDs generated")

    file_entries = []
    for path in files:
        material = path.name[:2]
        relative = path.relative_to(root).as_posix()
        matching = [record for record in records if path.name in record.source_file]
        if path.suffix.lower() == ".docx":
            sheets = [
                {
                    "sheet": "DOCX",
                    "header_row": None,
                    "fields": ["paragraphs", "tables.主题", "tables.观点摘要"],
                    "data_row_start": None,
                    "last_nonempty_row": None,
                    "nonempty_data_rows": len(matching),
                }
            ]
        else:
            sheets = _xlsx_manifest(path, material)
        file_entries.append(
            {
                "material": material,
                "input_path": relative,
                "file_name": path.name,
                "sha256": file_hashes[path],
                "sheets": sheets,
                "time_window": _file_window(material, path.name),
                "cleaning_rules": CLEANING_RULES[material],
                "evidence_record_count": len(matching),
                "evidence_locations": [
                    {
                        "id": record.id,
                        "sheet": record.source_sheet,
                        "field": record.source_field,
                        "source_row": record.source_row,
                    }
                    for record in matching
                ],
            }
        )
    evidence = {
        "schema_version": RAW_SCHEMA_VERSION,
        "dataset_version": version,
        "workspace_id": "demo",
        "source_periods": TIME_WINDOWS,
        "metrics": {
            "m1_session_roi": m1_metrics,
            "m2_genz": m2_metrics,
            "m3": m3_metrics,
            "m4": m4_metrics,
            "m5": m5_metrics,
        },
        "records": [record.model_dump(mode="json") for record in records],
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_version": version,
        "input_root": str(root),
        "files": file_entries,
        "cleaning_rules": CLEANING_RULES,
        "time_windows": TIME_WINDOWS,
        "totals": {
            "input_files": len(files),
            "xlsx_files": sum(path.suffix.lower() == ".xlsx" for path in files),
            "docx_files": sum(path.suffix.lower() == ".docx" for path in files),
            "evidence_records": len(records),
            "records_by_material": {
                material: sum(record.source_material == material for record in records)
                for material in sorted(EXPECTED_FILES)
            },
        },
    }
    return RawImportResult(evidence=evidence, manifest=manifest)


def write_raw_outputs(result: RawImportResult, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "evidence.raw.json"
    manifest_path = output_dir / "raw_manifest.json"
    evidence_path.write_text(
        json.dumps(result.evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence_path, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import original M1–M6 XLSX/DOCX without copying inputs")
    parser.add_argument("raw_root", type=Path, help="Root directory containing M1–M6 materials")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    result = import_raw_data(args.raw_root)
    evidence_path, manifest_path = write_raw_outputs(result, args.output_dir)
    print(
        json.dumps(
            {
                "dataset_version": result.evidence["dataset_version"],
                "evidence_records": len(result.evidence["records"]),
                "input_files": result.manifest["totals"]["input_files"],
                "evidence_output": str(evidence_path),
                "manifest_output": str(manifest_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
