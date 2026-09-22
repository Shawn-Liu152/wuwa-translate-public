#!/usr/bin/env python3
"""Holdout 视频 blind 运行（Phase 12）。

用法：
  python tools/run_holdout.py en|ja|ko [--dry-run]

每个语种一个全新视频（首次翻译前不做任何适配——任务书 #67 blind）：
  en: 6fN9ii-8U7s  (3.7 Trailer Reaction, YouTube 自动字幕 vs Whisper)
  ja: l9NCconvSNA  (2.6 Story Reaction)
  ko: a8ZSJPGGbbg  (2.7 官方放送 Highlights)
"""
from __future__ import annotations

import argparse
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
        "video_id": "6fN9ii-8U7s",
        "primary": BASE / "download" / "holdout-6fN9ii-8U7s" / "en.en.srt",
        "secondary": BASE / "download" / "holdout-6fN9ii-8U7s" / "reference.whisper.en.srt",
    },
    "ja": {
        "video_id": "l9NCconvSNA",
        "primary": BASE / "download" / "holdout-l9NCconvSNA" / "ja.ja.srt",
        "secondary": BASE / "download" / "holdout-l9NCconvSNA" / "reference.whisper.ja.srt",
    },
    "ko": {
        "video_id": "a8ZSJPGGbbg",
        "primary": BASE / "download" / "holdout-a8ZSJPGGbbg" / "ko.ko.srt",
        "secondary": BASE / "download" / "holdout-a8ZSJPGGbbg" / "reference.whisper.ko.srt",
    },
}

OPTIONS = {
    "model": "deepseek-v4-flash",
    "round2_model": "deepseek-v4-flash",
    "batch_size": 28,
    "game": "wuwa",
    "base_url": "https://opencode.ai/zen/go/v1",
    "proxy": "http://127.0.0.1:7890",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lang", choices=["en", "ja", "ko"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="从 manifest 恢复已成功批次")
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
    if not spec["secondary"].is_file():
        print(f"[blocked] secondary 不存在: {spec['secondary']}")
        return 1

    out_dir = BASE / "download" / f"holdout-{spec['video_id']}"
    final_path = out_dir / "final.zh.srt"
    print(f"===== [{args.lang}] holdout {spec['video_id']} (blind) =====")

    from pipeline.long_video import run_long_video

    t0 = time.time()
    code = run_long_video(
        str(spec["primary"]),
        str(spec["secondary"]),
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
    print(f"[{args.lang}] exit={code} elapsed={elapsed:.0f}s")
    summary_path = out_dir / "holdout_summary.json"
    summary_path.write_text(
        json.dumps({
            "lang": args.lang,
            "video_id": spec["video_id"],
            "exit": code,
            "elapsed_s": round(elapsed, 1),
            "blind": True,
            "adaptations": [],
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
