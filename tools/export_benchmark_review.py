# -*- coding: utf-8 -*-
"""Export benchmark entries to a human-review CSV without changing the gold set."""
import argparse
import csv
import json
from pathlib import Path


REVIEW_FIELDS = [
    "language",
    "id",
    "canonical_source_text",
    "current_gold_zh",
    "gold_status",
    "decision",
    "corrected_gold_zh",
    "review_notes",
    "evidence",
    "reviewer",
    "reviewed_at",
]


def export_review(benchmark_path, output_path, *, gold_status=None):
    benchmark_path = Path(benchmark_path)
    output_path = Path(output_path)
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    language = str(benchmark.get("source_language") or "").strip()
    if not language:
        raise ValueError("benchmark source_language is required")

    rows = []
    seen = set()
    for entry in benchmark.get("entries", []):
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise ValueError("benchmark entry id is required")
        if entry_id in seen:
            raise ValueError(f"duplicate benchmark id: {entry_id}")
        seen.add(entry_id)
        status = str(entry.get("gold_status") or "").strip()
        if gold_status is not None and status != gold_status:
            continue
        rows.append({
            "language": language,
            "id": entry_id,
            "canonical_source_text": str(
                entry.get("canonical_source_text") or ""
            ),
            "current_gold_zh": str(entry.get("gold_zh") or ""),
            "gold_status": status,
            "decision": "",
            "corrected_gold_zh": "",
            "review_notes": "",
            "evidence": "",
            "reviewer": "",
            "reviewed_at": "",
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "benchmark": str(benchmark_path.resolve()),
        "output": str(output_path.resolve()),
        "gold_status": gold_status,
        "row_count": len(rows),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Export a benchmark CSV for genuine human review."
    )
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--gold-status")
    args = parser.parse_args()
    result = export_review(
        args.benchmark, args.output, gold_status=args.gold_status,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
