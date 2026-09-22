#!/usr/bin/env python3
"""Summarize genuine review labels without inferring missing judgements."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from tools.prepare_review_batch import REVIEW_BATCH_FIELDS
except ModuleNotFoundError:  # Direct invocation from tools/.
    from prepare_review_batch import REVIEW_BATCH_FIELDS


REVIEW_DECISIONS = {
    "", "skip", "round1_preferred", "round2_preferred", "both_correct",
    "both_wrong", "source_error",
}
AUTOMATED_REVIEWERS = {
    "ai", "assistant", "chatgpt", "codex", "human", "llm", "model",
    "openai",
}


def _read_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != REVIEW_BATCH_FIELDS:
            raise ValueError("review CSV fields do not match the review batch schema")
        return list(reader)


def validate_review_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """Validate explicit labels before any caller persists or summarizes them."""
    reviewed = []
    seen_ids = set()
    changed_by_id = {}
    for row in rows:
        entry_id = row["id"].strip()
        if not entry_id:
            raise ValueError("review row id is required")
        if entry_id in seen_ids:
            raise ValueError(f"duplicate review id: {entry_id}")
        seen_ids.add(entry_id)
        changed_text = row["machine_changed"].strip().casefold()
        if changed_text not in {"true", "false"}:
            raise ValueError(f"{entry_id}: machine_changed must be true or false")
        changed_by_id[entry_id] = changed_text == "true"
        texts_differ = row["round1_zh"].strip() != row["round2_zh"].strip()
        if changed_by_id[entry_id] != texts_differ:
            raise ValueError(
                f"{entry_id}: machine_changed disagrees with the machine texts"
            )
        decision = row["review_decision"].strip().casefold()
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"{entry_id}: unknown review_decision: {decision}")
        if decision in {"", "skip"}:
            continue
        if (
            changed_by_id[entry_id] is False
            and decision in {"round1_preferred", "round2_preferred"}
        ):
            raise ValueError(
                f"{entry_id}: cannot prefer one of two identical machine texts"
            )
        reviewer = row["reviewer"].strip()
        if not reviewer or reviewer.casefold() in AUTOMATED_REVIEWERS:
            raise ValueError(f"{entry_id}: a genuine human reviewer is required")
        if not row["evidence"].strip():
            raise ValueError(f"{entry_id}: evidence is required")
        reviewed_at = row["reviewed_at"].strip()
        try:
            parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"{entry_id}: reviewed_at must be ISO-8601"
            ) from error
        if parsed.tzinfo is None:
            raise ValueError(f"{entry_id}: reviewed_at must include a timezone")
        if decision == "both_wrong" and not row["final_zh"].strip():
            raise ValueError(f"{entry_id}: final_zh is required for both_wrong")
        if decision == "source_error" and not row["source_quality"].strip():
            raise ValueError(f"{entry_id}: source_quality is required for source_error")
        reviewed.append({**row, "review_decision": decision})
    return reviewed, changed_by_id


def summarize_review_batch(
    review_csv: str | Path,
    *,
    output_json: str | Path,
    output_report: str | Path,
) -> dict[str, Any]:
    """Calculate only metrics supported by explicit genuine-human labels."""
    rows = _read_rows(review_csv)
    reviewed, changed_by_id = validate_review_rows(rows)

    decision_counts = Counter(row["review_decision"] for row in reviewed)
    translation_rows = [
        row for row in reviewed if row["review_decision"] != "source_error"
    ]
    changed_reviewed = [
        row for row in translation_rows if changed_by_id[row["id"].strip()]
    ]
    effective_fix_count = sum(
        row["review_decision"] == "round2_preferred"
        for row in changed_reviewed
    )
    harmful_change_count = sum(
        row["review_decision"] == "round1_preferred"
        for row in changed_reviewed
    )
    unfixed_count = sum(
        row["review_decision"] == "both_wrong"
        for row in changed_reviewed
    )
    changed_count = len(changed_reviewed)
    evaluable_count = len(translation_rows)
    round1_correct = sum(
        row["review_decision"] in {"round1_preferred", "both_correct"}
        for row in translation_rows
    )
    round2_correct = sum(
        row["review_decision"] in {"round2_preferred", "both_correct"}
        for row in translation_rows
    )

    def rate(numerator, denominator):
        return round(numerator / denominator, 6) if denominator else None

    result = {
        "schema_version": 1,
        "kind": "human_review_batch_metrics",
        "selected_count": len(rows),
        "reviewed_count": len(reviewed),
        "review_completion_rate": (
            round(len(reviewed) / len(rows), 6) if rows else None
        ),
        "decision_counts": dict(sorted(decision_counts.items())),
        "source_error_count": decision_counts["source_error"],
        "translation_evaluable_count": evaluable_count,
        "changed_reviewed_count": changed_count,
        "effective_fix_count": effective_fix_count,
        "harmful_change_count": harmful_change_count,
        "unfixed_count": unfixed_count,
        "effective_fix_rate": rate(effective_fix_count, changed_count),
        "harmful_change_rate": rate(harmful_change_count, changed_count),
        "round1_accuracy": rate(round1_correct, evaluable_count),
        "round2_accuracy": rate(round2_correct, evaluable_count),
        "effective_fix_rate_observation": (
            "human_observed" if changed_count else "not_observable"
        ),
    }
    json_path = Path(output_json)
    report_path = Path(output_report)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not reviewed:
        observation = (
            "No genuine human labels are present; correctness and effective "
            "fix rate remain not observable."
        )
    else:
        observation = (
            "Metrics below use only labels that passed the genuine-human "
            "review validation gate."
        )
    report_path.write_text(
        "\n".join([
            "# Human Review Batch Metrics",
            "",
            f"> {observation}",
            "",
            f"- Selected: {len(rows)}",
            f"- Reviewed: {len(reviewed)}",
            f"- Validated genuine-human labels: {len(reviewed)}",
            f"- Source errors: {decision_counts['source_error']}",
            f"- Changed examples reviewed: {changed_count}",
            f"- Effective fixes: {effective_fix_count}",
            f"- Harmful changes: {harmful_change_count}",
            f"- Effective fix rate: {result['effective_fix_rate']}",
            f"- Round 1 accuracy: {result['round1_accuracy']}",
            f"- Round 2 accuracy: {result['round2_accuracy']}",
            "",
        ]),
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize an exported review batch without guessing labels.",
    )
    parser.add_argument("review_csv", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = summarize_review_batch(
            args.review_csv,
            output_json=args.output_json,
            output_report=args.output_report,
        )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
