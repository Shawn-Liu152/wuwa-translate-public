#!/usr/bin/env python3
"""Final Blind holdout runner (2026-08-11 final round).

Single-source mode: if whisper secondary is missing, use an empty SRT
compat file (same as historical ja/ko acceptance jobs) so the pipeline
runs single-source while whisper transcription can be backfilled.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

JOBS = {
    "en": {
        "video_id": "oRkLMSh9F1Y",
        "primary": BASE / "download" / "finalblind-oRkLMSh9F1Y" / "en.en.srt",
        "secondary": BASE / "download" / "finalblind-oRkLMSh9F1Y" / "reference.whisper.en.srt",
    },
    "ja": {
        "video_id": "9UyrQE1qBDg",
        "primary": BASE / "download" / "finalblind-9UyrQE1qBDg" / "ja.ja.srt",
        "secondary": BASE / "download" / "finalblind-9UyrQE1qBDg" / "reference.whisper.ja.srt",
    },
    "ko": {
        "video_id": "C4Tf5vRMmGc",
        "primary": BASE / "download" / "finalblind-C4Tf5vRMmGc" / "ko.ko.srt",
        "secondary": BASE / "download" / "finalblind-C4Tf5vRMmGc" / "reference.whisper.ko.srt",
    },
}

EMPTY_SRT = BASE / "download" / "_empty_secondary.srt"
if not EMPTY_SRT.is_file():
    EMPTY_SRT.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")

OPTIONS = {
    "model": "deepseek-v4-flash",
    "round2_model": "deepseek-v4-flash",
    "batch_size": 28,
    "game": "wuwa",
    "base_url": "https://opencode.ai/zen/go/v1",
    "proxy": "http://127.0.0.1:7890",
}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("lang", choices=["en", "ja", "ko"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    api_key = (os.environ.get("LLM_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("API_KEY") or "")
    if not api_key and not args.dry_run:
        print("[blocked] .env 中未找到 API key")
        return 1

    spec = JOBS[args.lang]
    if not spec["primary"].is_file():
        print(f"[blocked] primary 不存在: {spec['primary']}")
        return 1
    secondary = spec["secondary"] if spec["secondary"].is_file() else EMPTY_SRT
    mode = "dual-source" if secondary != EMPTY_SRT else "single-source(empty secondary)"
    print(f"[finalblind] {args.lang} {spec['video_id']} mode={mode}")

    from pipeline.long_video import run_long_video

    out_dir = BASE / "download" / f"finalblind-{spec['video_id']}"
    final_path = out_dir / "final.zh.srt"
    t0 = time.time()
    code = run_long_video(
        str(spec["primary"]),
        str(secondary),
        str(final_path),
        model=OPTIONS["model"],
        api_key=api_key,
        batch_size=OPTIONS["batch_size"],
        game=OPTIONS["game"],
        resume=args.resume,
        base_url=OPTIONS["base_url"],
        proxy=OPTIONS["proxy"],
        round2_model=OPTIONS["round2_model"],
        source_language=args.lang,
        dry_run=args.dry_run,
    )
    elapsed = time.time() - t0
    print(f"[finalblind] exit={code} elapsed={elapsed:.0f}s")
    (out_dir / "holdout_summary.json").write_text(
        json.dumps({
            "lang": args.lang,
            "video_id": spec["video_id"],
            "exit": code,
            "elapsed_s": round(elapsed, 1),
            "blind": True,
            "mode": mode,
            "adaptations": [],
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    # Codex review fix: propagate real exit code (was hardcoded 0 -> false success)
    return code if code is not None else 1


if __name__ == "__main__":
    sys.exit(main())
