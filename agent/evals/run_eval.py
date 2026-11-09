from __future__ import annotations

import json
from pathlib import Path

from audience_agent.graph import classify_intent
from audience_agent.seeding import load_seed

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("cases.json")


def main() -> int:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    records = load_seed(ROOT / "data/evidence.raw.json")
    failures = []
    for item in cases:
        actual = classify_intent(item["question"])
        reason = None
        required_sources = item["must_cite"]
        if isinstance(required_sources, str):
            required_sources = [required_sources]
        if actual != item["expected_intent"]:
            reason = f"intent mismatch: expected={item['expected_intent']}, actual={actual}"
        elif actual is not None and any(
            not any(
                record.source_material == source
                and (actual != "cross_domain" or record.domain in {
                    "audience_profile", "experience", "fulfillment", "cross_domain"
                })
                for record in records
            )
            for source in required_sources or []
        ):
            reason = f"source coverage missing: {required_sources}"
        if reason:
            failures.append({"question": item["question"], "reason": reason})
    print(json.dumps({"cases": len(cases), "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
