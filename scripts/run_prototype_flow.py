#!/usr/bin/env python3
"""Run and record the real local API flow: question -> follow-up -> evidence -> feedback."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def post(base: str, path: str, token: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def require_ok(status: int, body: dict, step: str) -> None:
    if status not in {200, 202}:
        raise RuntimeError(f"{step} failed with HTTP {status}: {json.dumps(body, ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--workspace", default="demo")
    parser.add_argument("--token", default="local-query-token")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prefix = f"/v1/workspaces/{args.workspace}"
    question = "请联合品牌A的Gen Z画像、穿着体验、履约信号和专家观点，形成可追溯的人群假设；漾YOUNG应如何验证？"
    followup = "这些假设有哪些反例或替代解释？请指出证据不足之处，并给出下周最小验证动作。"
    started = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()

    status1, first = post(args.base, prefix + "/insights", args.token, {"question": question})
    require_ok(status1, first, "initial insight")
    thread_id = first["thread_id"]
    status2, second = post(
        args.base,
        prefix + "/insights",
        args.token,
        {"question": followup, "thread_id": thread_id},
    )
    require_ok(status2, second, "follow-up insight")
    if second.get("thread_id") != thread_id:
        raise AssertionError("follow-up did not retain thread_id")
    if first.get("status") != "answered" or second.get("status") != "answered":
        raise AssertionError("business flow requires answered initial and follow-up responses")
    if not first.get("dataset_version") or not second.get("dataset_version"):
        raise AssertionError("both responses must bind an active dataset_version")
    materials = {
        card.get("source_material", "")
        for response in (first, second)
        for card in response.get("evidence", [])
    }
    missing_domains = {"M2", "M3", "M4", "M5"} - materials
    if missing_domains:
        raise AssertionError(f"cross-domain evidence coverage missing: {sorted(missing_domains)}")

    cited = {card["id"] for card in first.get("evidence", [])}
    for claim in first.get("claims", []):
        if not set(claim.get("evidence_ids", [])) <= cited:
            raise AssertionError("response contains citation outside returned evidence cards")
    if first.get("claims") and not cited:
        raise AssertionError("answered response has no evidence cards")

    status3, feedback = post(
        args.base,
        prefix + "/feedback",
        args.token,
        {
            "run_id": second["run_id"],
            "thread_id": thread_id,
            "dataset_version": second["dataset_version"],
            "decision": "needs_validation",
            "notes": "会前结论保留为待验证；下周补齐YOUNG一手访谈、评论与SKU级结果。",
        },
    )
    require_ok(status3, feedback, "feedback")

    result = {
        "started_at": started,
        "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "base_url": args.base,
        "workspace": args.workspace,
        "llm_mode": "mock (deterministic local demonstration; not real-model quality evidence)",
        "question": question,
        "initial": first,
        "followup_question": followup,
        "followup": second,
        "feedback": feedback,
        "checks": {
            "same_thread": True,
            "answered_initial_and_followup": True,
            "dataset_bound_on_both_turns": True,
            "cross_domain_materials": sorted(materials),
            "citations_within_returned_cards": True,
            "feedback_http_status": status3,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 原型真实运行摘要",
        "",
        f"- 时间：{result['started_at']}（Asia/Shanghai）",
        f"- 地址：{args.base}/prototype",
        "- 模式：memory + deterministic mock；不是生产或真实模型质量证明",
        f"- thread_id：`{thread_id}`",
        f"- dataset_version：`{second['dataset_version']}`",
        f"- 首问状态：{first.get('status')}；证据卡：{len(first.get('evidence', []))}",
        f"- 追问状态：{second.get('status')}；证据卡：{len(second.get('evidence', []))}",
        f"- 反馈：needs_validation；HTTP {status3}",
        "",
        "## 首问主张与证据",
    ]
    for claim in first.get("claims", []):
        lines.append(f"- {claim['text']}（{claim['confidence']}；{', '.join(claim['evidence_ids'])}）")
    lines.extend(["", "## 追问主张与证据"])
    for claim in second.get("claims", []):
        lines.append(f"- {claim['text']}（{claim['confidence']}；{', '.join(claim['evidence_ids'])}）")
    lines.extend(["", "## 限制", ""] + [f"- {x}" for x in second.get("limitations", [])])
    lines.extend(["", "## 下一步", ""] + [f"- {x}" for x in second.get("next_steps", [])])
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "thread_id": thread_id, "status": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
