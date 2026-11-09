from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from openpyxl import load_workbook

from audience_agent.raw_ingest import import_raw_data, write_raw_outputs

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = Path(os.environ["RAW_DATA_ROOT"]) if os.environ.get("RAW_DATA_ROOT") else None
requires_raw = pytest.mark.skipif(
    RAW_ROOT is None or not RAW_ROOT.is_dir(), reason="set RAW_DATA_ROOT to run original-data integration"
)


@pytest.fixture(scope="session")
def bundled():
    return json.loads((ROOT / "data/evidence.raw.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def bundled_manifest():
    return json.loads((ROOT / "data/raw_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def regenerated():
    if RAW_ROOT is None or not RAW_ROOT.is_dir():
        pytest.skip("set RAW_DATA_ROOT to regenerate original data")
    return import_raw_data(RAW_ROOT)


def record(payload, record_id):
    return next(item for item in payload["records"] if item["id"] == record_id)


def test_raw_m1_verified_roi_values_are_preserved(bundled):
    metrics = bundled["metrics"]["m1_session_roi"]
    assert metrics["session_dates"] == 12
    assert metrics["joined_natural_day_rows"] == 13
    assert metrics["eligible_rows"] == 12
    assert metrics["total_spend_yuan"] == pytest.approx(5_067_010.68)
    assert metrics["total_overall_gmv_yuan"] == pytest.approx(19_398_933.07)
    assert metrics["weighted_overall_roi"] == pytest.approx(3.8284768466286314)
    assert metrics["unweighted_reported_daily_roi_mean"] == pytest.approx(3.7483333333333335)
    assert metrics["excluded_rows"][0]["source_row"] == 14


def test_raw_m2_has_24_sessions_23_dates_and_age_band_metrics(bundled):
    metrics = bundled["metrics"]["m2_genz"]
    assert metrics["exposure"]["mean"] == pytest.approx(0.012669568534649015)
    assert metrics["interaction"]["mean"] == pytest.approx(0.010750635012445657)
    assert metrics["qualified"]["mean"] == pytest.approx(0.0058882391491815525)
    assert metrics["exposure"]["age_18_30"]["mean"] == pytest.approx(0.154801, abs=1e-6)
    assert metrics["interaction"]["age_18_30"]["mean"] == pytest.approx(0.159730, abs=1e-6)
    assert metrics["qualified"]["age_18_30"]["mean"] == pytest.approx(0.125194, abs=1e-6)
    evidence = record(bundled, "M2-GENZ-24-SESSION-MEAN")
    assert evidence["metadata"]["session_count"] == 24
    assert evidence["metadata"]["date_count"] == 23
    assert evidence["metadata"]["duplicate_dates"] == {"2026-06-19": 2}
    assert len(evidence["source_file"].splitlines()) == 72
    assert "24场合并" not in evidence["source_file"]


def test_raw_m4_verified_totals_amounts_and_association_rates(bundled):
    metrics = bundled["metrics"]["m4"]
    assert metrics["product_rows"] == 147_142
    assert metrics["parent_orders"] == 145_472
    assert metrics["paid_orders"] == 131_358
    assert metrics["warehouse_issue_orders"] == 1_074
    assert metrics["resend_orders"] == 128
    assert metrics["issue_rate"] == pytest.approx(0.008176129356415292)
    assert metrics["resend_rate"] == pytest.approx(0.000974436273390277)
    assert metrics["paid_parent_order_amount"] == pytest.approx(24_798_701.62)
    assert metrics["paid_product_row_amount"] == pytest.approx(25_046_478.06)
    assert metrics["resend_association_fields"] == ["线上订单号", "原始线上订单号"]


def test_cross_domain_issue_and_voc_theme_counts_are_exact(bundled):
    m4 = bundled["metrics"]["m4"]
    assert m4["issue_level2_paid_order_counts"]["J卷边"] == 66
    assert m4["issue_level2_paid_order_counts"]["P跑杯"] == 11
    assert m4["issue_level2_paid_order_counts"]["不显小"] == 43
    assert m4["issue_level2_paid_order_counts"]["C承托力不足"] == 23
    assert m4["issue_level1_paid_order_counts"]["尺码问题"] == 327
    assert m4["issue_level1_paid_order_counts"]["商品问题"] == 504
    topics = bundled["metrics"]["m3"]["july_topic_counts"]
    assert topics["产品功能"] == 51
    assert topics["尺码问题"] == 16
    assert topics["舒适体验"] == 12
    assert sum(topics.values()) == 100
    assert bundled["metrics"]["m3"]["july_verbatim_quotes"] == 99


def test_m3_second_quote_is_complete_and_has_real_cell(bundled):
    evidence = next(
        item
        for item in bundled["records"]
        if item["metadata"].get("source_cell") == "L13"
    )
    assert evidence["source_row"] == "汇总!L13"
    assert evidence["metadata"]["verbatim_quote"].endswith(
        "内衣洗了，短时间试穿体验不出啥来，多穿一会儿做些大动作就能都发现了"
    )
    assert bundled["metrics"]["m3"]["full_l13_verified"] is True


def test_m4_m6_real_file_names_and_runtime_windows_are_preserved(bundled):
    m4 = record(bundled, "M4-WAREHOUSE-ORDER-RATE")
    assert "M4-03A_全量订单分母汇总与关联说明_2026-06-01至2026-06-18_品牌A_脱敏版.xlsx" in m4["source_file"]
    m6 = record(bundled, "M2-NON-NESTED-CAVEAT")
    assert m6["source_file"].endswith("脱敏版(1).xlsx")
    assert "右删失" in bundled["source_periods"]["M4"]
    assert "2026-05+2026-06" in bundled["source_periods"]["M1"]
    assert record(bundled, "M4-RIGHT-CENSORING-CAVEAT")["metadata"]["right_censored"]


def test_m5_can_only_be_opinion_or_hypothesis(bundled):
    m5 = [item for item in bundled["records"] if item["source_material"] == "M5"]
    assert len(m5) == 31
    assert {item["record_type"] for item in m5} <= {"opinion", "hypothesis"}
    assert all(item["confidence"] == "low" for item in m5)
    assert all(item["domain"] == "cross_domain" for item in m5)


def test_manifest_has_all_inputs_hashes_fields_rows_and_cleaning_rules(bundled_manifest):
    assert bundled_manifest["totals"]["input_files"] == 87
    assert bundled_manifest["totals"]["xlsx_files"] == 86
    assert bundled_manifest["totals"]["docx_files"] == 1
    for item in bundled_manifest["files"]:
        assert item["input_path"]
        assert len(item["sha256"]) == 64
        assert item["sheets"]
        assert item["time_window"]
        assert item["cleaning_rules"]
        for sheet in item["sheets"]:
            assert "fields" in sheet
            assert "header_row" in sheet
            assert "last_nonempty_row" in sheet


@requires_raw
def test_regenerated_raw_and_manifest_match_committed_outputs(regenerated, bundled, bundled_manifest):
    assert regenerated.evidence == bundled
    assert regenerated.manifest == bundled_manifest


@requires_raw
def test_original_l13_and_manifest_sha256_are_independently_verified(regenerated):
    monthly = next(RAW_ROOT.rglob("M3-01*.xlsx"))
    workbook = load_workbook(monthly, read_only=True, data_only=True)
    try:
        raw_quote = workbook["汇总"]["L13"].value
    finally:
        workbook.close()
    generated_quote = next(
        item["metadata"]["verbatim_quote"]
        for item in regenerated.evidence["records"]
        if item["metadata"].get("source_cell") == "L13"
    )
    assert generated_quote == raw_quote
    for item in regenerated.manifest["files"]:
        path = RAW_ROOT / item["input_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


@requires_raw
def test_raw_output_bytes_are_reproducible(regenerated, tmp_path):
    first = write_raw_outputs(regenerated, tmp_path / "first")
    second = write_raw_outputs(regenerated, tmp_path / "second")
    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes()
