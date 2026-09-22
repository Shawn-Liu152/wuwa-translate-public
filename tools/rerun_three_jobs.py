#!/usr/bin/env python3
"""真实重跑三个审计 Job（OLD vs NEW），输出到独立 rerun 目录。

用法：python tools/rerun_three_jobs.py [--only en|ja|ko] [--dry-run]

环境要求：.env 中 API key（由 python-dotenv 加载）已配置。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

# 加载 .env（与 web 应用一致）
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    print("[warn] python-dotenv 不可用，尝试直接读 .env")
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

JOBS = {
    "en": {
        "job_id": "2519dcb3843e",
        "primary": BASE / "download" / "2519dcb3843e" / "过程文件" / "youtube.aligned.en.srt",
        "secondary": BASE / "download" / "2519dcb3843e" / "过程文件" / "reference.whisper.en.srt",
        "options": {"model": "deepseek-v4-flash", "round2_model": "deepseek-v4-flash",
                    "batch_size": 28, "game": "wuwa", "source_language": "en",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "proxy": "http://127.0.0.1:7890"},
    },
    "ja": {
        "job_id": "df93e43d0c47",
        "primary": BASE / "download" / "df93e43d0c47" / "过程文件" / "gl8vjOL1xWI.ja.cleaned.srt",
        "secondary": BASE / "download" / "df93e43d0c47" / "过程文件" / "reference.secondary.empty.ja.srt",
        "options": {"model": "deepseek-v4-flash", "round2_model": "deepseek-v4-flash",
                    "batch_size": 28, "game": "wuwa", "source_language": "ja",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "proxy": "http://127.0.0.1:7890"},
    },
    "ko": {
        "job_id": "2170758a3e78",
        "primary": BASE / "download" / "2170758a3e78" / "过程文件" / "p0bHZydkDrA.ko.cleaned.srt",
        "secondary": BASE / "download" / "2170758a3e78" / "过程文件" / "reference.secondary.empty.ko.srt",
        "options": {"model": "deepseek-v4-flash", "round2_model": "deepseek-v4-flash",
                    "batch_size": 28, "game": "wuwa", "source_language": "ko",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "proxy": "http://127.0.0.1:7890"},
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["en", "ja", "ko"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api_key = (os.environ.get("LLM_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("API_KEY") or "")
    if not api_key and not args.dry_run:
        print("[blocked] .env 中未找到 DEEPSEEK_API_KEY/API_KEY，无法真实调用 LLM")
        return 1
    print(f"[info] api_key 配置: {'yes' if api_key else 'no (dry-run)'}")

    from pipeline.long_video import run_long_video

    results = {}
    for lang, spec in JOBS.items():
        if args.only and lang != args.only:
            continue
        out_dir = BASE / "download" / f"rerun-{lang}-20260810"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        # run_long_video 的 output 参数是最终 SRT 文件路径（stage 产物
        # 围绕它生成：round1/round2/union 等同目录同 basename）
        final_path = out_dir / "final.zh.srt"
        print(f"\n===== [{lang}] rerun -> {out_dir} =====")
        t0 = time.time()
        code = run_long_video(
            str(spec["primary"]),
            str(spec["secondary"]),
            str(final_path),
            model=spec["options"]["model"],
            api_key=api_key,
            batch_size=spec["options"]["batch_size"],
            game=spec["options"]["game"],
            resume=False,
            base_url=spec["options"]["base_url"],
            proxy=spec["options"]["proxy"],
            round2_model=spec["options"]["round2_model"],
            source_language=spec["options"]["source_language"],
            dry_run=args.dry_run,
        )
        elapsed = time.time() - t0
        print(f"[{lang}] exit={code} elapsed={elapsed:.0f}s")
        results[lang] = {"exit": code, "elapsed_s": round(elapsed, 1),
                         "out_dir": str(out_dir)}
        if code != 0:
            print(f"[{lang}] 运行失败，继续下一个")

    (BASE / "docs" / "audit_tables" / "rerun_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n汇总:", json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
