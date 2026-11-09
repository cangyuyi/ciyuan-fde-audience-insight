#!/usr/bin/env python3
"""Validate and package the complete FDE delivery without local caches or raw inputs."""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ZIP_PATH = ROOT.parent / "词元引擎FDE_实战题_王泽栋_V2.0.zip"
PACKAGE_ROOT = "词元引擎FDE_实战题_王泽栋_V2.0"
REQUIRED = [
    "README_回传说明.md",
    "deliverables/01_词元引擎FDE_人群洞察分析报告_王泽栋.docx",
    "deliverables/01_词元引擎FDE_人群洞察分析报告_王泽栋.pdf",
    "deliverables/02_词元引擎FDE_六栏一页纸_王泽栋.docx",
    "deliverables/02_词元引擎FDE_六栏一页纸_王泽栋.pdf",
    "deliverables/03_词元引擎FDE_产品方案架构图.html",
    "deliverables/03_词元引擎FDE_产品方案架构图.png",
    "deliverables/04_词元引擎FDE_采用与风险说明_王泽栋.docx",
    "deliverables/04_词元引擎FDE_采用与风险说明_王泽栋.pdf",
    "agent/data/evidence.raw.json",
    "agent/data/raw_manifest.json",
    "agent/docs/openapi.json",
    "prototype/index.html",
    "prototype/app.js",
    "prototype/insights.json",
    "evidence/insights.json",
    "evidence/meeting_snapshot.json",
    "evidence/prototype_run.json",
    "evidence/screenshots/capture_manifest.json",
    "qa/pytest.log",
    "qa/ruff.log",
    "qa/eval.log",
    "qa/数据复核.md",
    "qa/内容与视觉验收.md",
    "qa/后端独立复审.md",
    "qa/后端独立复审_整改确认.md",
    "qa/最终验收.md",
]
QA_EXTRA = {"qa/prototype_flow.log", "qa/capture.log"}
EXCLUDE_PARTS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".chrome-home", "chrome-profile-"}


def included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return False
    if path.name in {".DS_Store"} or path.suffix == ".pyc":
        return False
    if rel.parts[0] == "qa":
        return str(rel) in set(REQUIRED) | QA_EXTRA
    return rel.parts[0] in {"agent", "prototype", "deliverables", "evidence", "scripts"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
    if missing:
        raise RuntimeError(f"missing required deliverables: {missing}")
    for rel in REQUIRED:
        if (ROOT / rel).stat().st_size == 0:
            raise RuntimeError(f"empty required deliverable: {rel}")
    for rel in [x for x in REQUIRED if x.endswith(".pdf")]:
        if not (ROOT / rel).read_bytes().startswith(b"%PDF-"):
            raise RuntimeError(f"invalid PDF signature: {rel}")
    for rel in [x for x in REQUIRED if x.endswith(".json")]:
        json.loads((ROOT / rel).read_text(encoding="utf-8"))
    flow = json.loads((ROOT / "evidence/prototype_run.json").read_text(encoding="utf-8"))
    if not flow["checks"].get("answered_initial_and_followup"):
        raise RuntimeError("prototype flow did not answer both turns")
    if not {"M2", "M3", "M4", "M5"} <= set(flow["checks"].get("cross_domain_materials", [])):
        raise RuntimeError("prototype flow lacks cross-domain evidence")
    shots = list((ROOT / "evidence/screenshots").glob("*.png"))
    if len(shots) < 5:
        raise RuntimeError("five prototype screenshots are required")

    files = [ROOT / "README_回传说明.md", ROOT / ".gitignore"]
    files.extend(p for p in ROOT.rglob("*") if p.is_file() and included(p))
    files.extend(ROOT / rel for rel in QA_EXTRA if (ROOT / rel).exists())
    files = sorted(set(files), key=lambda p: str(p.relative_to(ROOT)))
    payloads: dict[str, bytes] = {}
    for path in files:
        rel = str(path.relative_to(ROOT))
        size = path.stat().st_size
        if size > 50 * 1024 * 1024:
            raise RuntimeError(f"single file exceeds 50MB: {rel}")
        if rel == "agent/data/raw_manifest.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["input_root"] = "M1-M6"
            payloads[rel] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        else:
            payloads[rel] = path.read_bytes()

    sums = "".join(f"{sha(data)}  {rel}\n" for rel, data in sorted(payloads.items())).encode("utf-8")
    payloads["SHA256SUMS.txt"] = sums
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel, data in sorted(payloads.items()):
            archive.writestr(f"{PACKAGE_ROOT}/{rel}", data)
    with zipfile.ZipFile(ZIP_PATH) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise RuntimeError(f"zip CRC failure: {corrupt}")
        names = set(archive.namelist())
        for rel in REQUIRED:
            if f"{PACKAGE_ROOT}/{rel}" not in names:
                raise RuntimeError(f"required file missing from zip: {rel}")

    receipt = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "zip": str(ZIP_PATH),
        "package_root": PACKAGE_ROOT,
        "file_count": len(payloads),
        "zip_bytes": ZIP_PATH.stat().st_size,
        "zip_sha256": sha(ZIP_PATH.read_bytes()),
        "required_files": REQUIRED,
        "prototype_screenshots": len(shots),
        "validation": {
            "zip_crc": "passed",
            "required_files": "passed",
            "pdf_signatures": "passed",
            "json_parse": "passed",
            "single_file_under_50mb": "passed",
            "prototype_business_flow": "passed",
        },
    }
    (ROOT / "qa/交付清单.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
