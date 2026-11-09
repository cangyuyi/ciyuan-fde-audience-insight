#!/usr/bin/env python3
"""Capture real browser states from the running prototype using installed Chrome."""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RuntimeError(f"invalid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/prototype/")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not Path(CHROME).exists():
        raise RuntimeError(f"Chrome not found: {CHROME}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    question = "请联合品牌A的Gen Z画像、穿着体验、履约信号和专家观点，形成可追溯的人群假设；漾YOUNG应如何验证？"
    query_question = urllib.parse.urlencode({"autorun": "1", "question": question})
    shots = [
        ("01_会前工作台.png", args.base, 1000),
        ("02_首问与证据.png", args.base + "?" + query_question, 2400),
        ("03_同线程追问与反方校验.png", args.base + "?scenario=full", 2400),
        ("04_反馈待验证.png", args.base + "?scenario=feedback&decision=needs_validation", 2400),
        ("05_会前一页纸.png", args.base + "?view=brief", 2400),
    ]
    manifest = []
    for filename, url, height in shots:
        target = args.output_dir / filename
        with tempfile.TemporaryDirectory(prefix="chrome-profile-", dir=args.output_dir) as profile:
            env = os.environ.copy()
            env["HOME"] = str(args.output_dir / ".chrome-home")
            env["XDG_CONFIG_HOME"] = str(args.output_dir / ".chrome-home" / ".config")
            Path(env["HOME"]).mkdir(exist_ok=True)
            command = [
                CHROME,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--disable-background-networking",
                "--no-first-run",
                "--force-device-scale-factor=1",
                f"--window-size=1440,{height}",
                "--virtual-time-budget=15000",
                f"--user-data-dir={profile}",
                f"--screenshot={target}",
                url,
            ]
            completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=75)
        if completed.returncode != 0:
            raise RuntimeError(f"Chrome failed for {url}: {completed.stderr[-2000:]}")
        if not target.exists() or target.stat().st_size < 10_000:
            raise RuntimeError(f"missing or suspicious screenshot: {target}")
        width, image_height = png_size(target)
        if (width, image_height) != (1440, height):
            raise AssertionError(f"unexpected dimensions for {target}: {(width, image_height)}")
        manifest.append({"file": filename, "url": url, "width": width, "height": height, "bytes": target.stat().st_size})
        print(f"captured {filename} ({target.stat().st_size} bytes)")
    receipt = {
        "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "browser": CHROME,
        "mode": "headless Chrome against the live local FastAPI prototype",
        "shots": manifest,
    }
    (args.output_dir / "capture_manifest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
