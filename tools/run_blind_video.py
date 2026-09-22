#!/usr/bin/env python3
"""Run an arbitrary, single-source blind subtitle job.

This is the reusable counterpart to the fixed historical holdout runners.  It
never invokes Whisper: callers provide an original-language SRT and the
pipeline receives an empty compatibility secondary source.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE / ".env")
    except ImportError:
        env_file = BASE / ".env"
        if not env_file.is_file():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a new EN/JA/KO blind job from original-language SRT"
    )
    parser.add_argument("lang", choices=["en", "ja", "ko"])
    parser.add_argument("video_id")
    parser.add_argument("primary", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    primary = args.primary.resolve()
    if not primary.is_file():
        parser.error(f"primary SRT does not exist: {primary}")

    _load_env()
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("API_KEY")
        or ""
    )
    if not api_key and not args.dry_run:
        print("[blocked] .env 中未找到 API key")
        return 1

    out_dir = (args.out_dir or BASE / "download" / f"blind-{args.video_id}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    empty_secondary = BASE / "download" / "_empty_secondary.srt"
    if not empty_secondary.is_file():
        empty_secondary.parent.mkdir(parents=True, exist_ok=True)
        empty_secondary.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8"
        )

    from pipeline.long_video import run_long_video

    raw_code = run_long_video(
        str(primary),
        str(empty_secondary),
        str(out_dir / "final.zh.srt"),
        model="deepseek-v4-flash",
        api_key=api_key,
        batch_size=28,
        game="wuwa",
        resume=args.resume,
        base_url="https://opencode.ai/zen/go/v1",
        proxy="http://127.0.0.1:7890",
        round2_model="deepseek-v4-flash",
        source_language=args.lang,
        dry_run=args.dry_run,
    )
    exit_code = 0 if raw_code in (0, None) else int(raw_code)
    summary = {
        "lang": args.lang,
        "video_id": args.video_id,
        "exit": exit_code,
        "blind": True,
        "mode": "single-source(empty secondary)",
        "primary": str(primary),
        "adaptations": [],
    }
    (out_dir / "holdout_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
