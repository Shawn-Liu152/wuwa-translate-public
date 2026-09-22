"""O-1 redirect: offline triage measurement (read-only, zero API).

Reads the be479752b816 artifact trio, runs the production merge_risk_views
(including the O-1 triage) and reports how the human review queue shrinks.

Run:  unset PYTHONPATH && .venv/Scripts/python.exe tools/analyze_o1_triage.py
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.risk_queue_store import merge_risk_views  # noqa: E402

ART = ROOT / "download" / "be479752b816" / "过程文件"
QUEUE_STATES = {"pending", "review", "failed"}


def load(name: str):
    payload = json.loads((ART / name).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload.get("items", [])
    return payload


def reason_kinds(item: dict) -> list[str]:
    return [str(r).split(":", 1)[0] for r in item.get("reasons") or []]


def main() -> int:
    generated = load("final.zh.risk.generated.json")
    round2 = load("final.zh.round2.results.json")
    review = load("final.zh.review-state.json")

    merged = merge_risk_views(generated, round2, review)

    before = [
        it for it in merge_risk_views(generated, round2, review)
        if it.get("review_required")
    ]
    queue = [
        it for it in merged
        if it.get("review_required") and it.get("state") in QUEUE_STATES
    ]

    print(f"generated={len(generated)}  round2={len(round2)}  review={len(review)}")
    print(f"human queue (after triage) = {len(queue)}   [baseline 454]")
    print(f"review_required flag still set on {len(before)} entries -> unchanged")
    print(f"state histogram: {dict(collections.Counter(i['state'] for i in merged))}")

    reasons = collections.Counter()
    spot = 0
    for it in merged:
        if it.get("auto_resolved"):
            reasons[it.get("auto_resolved_reason", "")] += 1
        if it.get("spot_check"):
            spot += 1
    print(f"\nauto-resolved by strategy: {dict(reasons)}")
    print(f"held back by 5% spot check: {spot}")

    remaining = collections.Counter()
    for it in queue:
        for k in dict.fromkeys(reason_kinds(it)):
            remaining[k] += 1
    print("\nremaining queue by reason (overlapping):")
    for k, n in remaining.most_common():
        print(f"  {k:30s} {n}")

    hard = [it for it in queue if it.get("auto_resolved") is False
            and not it.get("spot_check")]
    print(f"\nentries needing genuine human work: {len(hard)}")

    samples = [it for it in merged if it.get("auto_resolved")][:4]
    print("\n--- auto-resolved samples ---")
    for it in samples:
        print(
            f"[{it['auto_resolved_reason']}] {it.get('source_text', '')[:60]!r}"
            f" -> {it.get('translated', '')[:40]!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
