#!/usr/bin/env python3
"""Recompute the exact few-shot selection for persisted real blind batches."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline.translate.prompt_builder import load_prompt_builder


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lang", choices=("en", "ja", "ko"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    memory = json.loads(
        (BASE / "data" / "translation_memory.json").read_text(encoding="utf-8")
    )["entries"]
    builder = load_prompt_builder(source_language=args.lang)
    batches = []
    for batch_id, batch in sorted(
        manifest["batches"].items(), key=lambda item: int(item[0])
    ):
        subtitles = []
        for item in batch.get("results", []):
            from pipeline.parser.srt_parser import Subtitle
            subtitles.append(Subtitle(
                int(item["id"]), "00:00:00,000", "00:00:01,000",
                str(item["original"]),
            ))
        batches.append({
            "batch": int(batch_id),
            **builder.few_shot_diagnostics(subtitles, memory),
        })
    report = {
        "language": args.lang,
        "manifest": str(args.manifest.resolve()),
        "approved_memory_count": sum(
            entry.get("approved") and entry.get("source_language") == args.lang
            for entry in memory
        ),
        "batches": batches,
    }
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "language": args.lang,
        "batch_count": len(batches),
        "batches_with_hits": sum(b["selected_count"] > 0 for b in batches),
        "selected_total": sum(b["selected_count"] for b in batches),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
