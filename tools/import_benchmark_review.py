# -*- coding: utf-8 -*-
"""Validate and optionally apply a genuine human benchmark review CSV."""
import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools.export_benchmark_review import REVIEW_FIELDS
except ModuleNotFoundError:  # Direct invocation: python tools/import_...py
    from export_benchmark_review import REVIEW_FIELDS


AUTOMATED_REVIEWERS = {
    "ai", "assistant", "chatgpt", "codex", "human", "llm", "model",
    "openai",
}
DECISIONS = {"", "skip", "keep", "correct", "reject"}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _validate_human_fields(row, entry_id):
    reviewer = row["reviewer"].strip()
    if not reviewer or reviewer.casefold() in AUTOMATED_REVIEWERS:
        raise ValueError(f"{entry_id}: a genuine human reviewer is required")
    evidence = row["evidence"].strip()
    if not evidence:
        raise ValueError(f"{entry_id}: evidence is required")
    reviewed_at = row["reviewed_at"].strip()
    if not reviewed_at:
        raise ValueError(f"{entry_id}: reviewed_at is required")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{entry_id}: reviewed_at must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{entry_id}: reviewed_at must include a timezone")
    return reviewer, evidence, reviewed_at


def _read_review(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != REVIEW_FIELDS:
            raise ValueError(
                "review CSV fields do not match the exported review schema"
            )
        return list(reader)


def _same_completed_review(entry, desired, row):
    review = entry.get("gold_review") or {}
    return (
        str(entry.get("gold_zh") or "") == desired["gold_zh"]
        and str(entry.get("gold_status") or "") == desired["gold_status"]
        and review.get("reviewer") == row["reviewer"].strip()
        and review.get("reviewed_at") == row["reviewed_at"].strip()
        and review.get("evidence") == row["evidence"].strip()
        and review.get("notes", "") == row["review_notes"].strip()
    )


def import_review(benchmark_path, review_path, ledger_path, *, apply=False):
    benchmark_path = Path(benchmark_path)
    review_path = Path(review_path)
    ledger_path = Path(ledger_path)
    benchmark_before = benchmark_path.read_bytes()
    benchmark = json.loads(benchmark_before.decode("utf-8"))
    language = str(benchmark.get("source_language") or "").strip()
    entries = benchmark.get("entries", [])
    by_id = {str(entry.get("id") or ""): entry for entry in entries}
    if len(by_id) != len(entries):
        raise ValueError("benchmark contains missing or duplicate ids")

    rows = _read_review(review_path)
    row_ids = set()
    changes = []
    already_applied = []
    for row in rows:
        entry_id = row["id"].strip()
        if entry_id in row_ids:
            raise ValueError(f"duplicate review id: {entry_id}")
        row_ids.add(entry_id)
        if entry_id not in by_id:
            raise ValueError(f"unknown benchmark id: {entry_id}")
        if row["language"].strip() != language:
            raise ValueError(f"{entry_id}: language mismatch")
        entry = by_id[entry_id]
        if row["canonical_source_text"] != str(
            entry.get("canonical_source_text") or ""
        ):
            raise ValueError(f"{entry_id}: stale canonical_source_text")
        decision = row["decision"].strip().casefold()
        if decision not in DECISIONS:
            raise ValueError(f"{entry_id}: unknown decision: {decision}")
        if decision in {"", "skip"}:
            continue

        reviewer, evidence, reviewed_at = _validate_human_fields(row, entry_id)
        current_gold = str(entry.get("gold_zh") or "")
        if decision == "correct":
            desired_gold = row["corrected_gold_zh"].strip()
            if not desired_gold:
                raise ValueError(
                    f"{entry_id}: corrected_gold_zh is required for correct"
                )
            desired_status = "confirmed"
        elif decision == "keep":
            desired_gold = current_gold
            if not desired_gold:
                raise ValueError(f"{entry_id}: current gold_zh is empty")
            desired_status = "confirmed"
        else:
            desired_gold = current_gold
            desired_status = "rejected"
        desired = {"gold_zh": desired_gold, "gold_status": desired_status}

        if _same_completed_review(entry, desired, row):
            already_applied.append(entry_id)
            continue
        if row["gold_status"].strip() != str(
            entry.get("gold_status") or ""
        ):
            raise ValueError(f"{entry_id}: stale gold_status")
        if row["current_gold_zh"] != current_gold:
            raise ValueError(f"{entry_id}: stale current_gold_zh")

        before = {
            "gold_zh": current_gold,
            "gold_status": str(entry.get("gold_status") or ""),
        }
        entry["gold_zh"] = desired_gold
        entry["gold_status"] = desired_status
        entry["gold_review"] = {
            "evidence": evidence,
            "notes": row["review_notes"].strip(),
            "reviewed_at": reviewed_at,
            "reviewer": reviewer,
        }
        changes.append({
            "id": entry_id,
            "decision": decision,
            "before": before,
            "after": desired,
            "evidence": evidence,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
        })

    projected_benchmark = _json_bytes(benchmark)
    benchmark_after = benchmark_before
    if apply and changes:
        _atomic_write(benchmark_path, projected_benchmark)
        benchmark_after = projected_benchmark

    timestamp = datetime.now(timezone.utc).isoformat()
    ledger_record = {
        "timestamp": timestamp,
        "applied": bool(apply),
        "benchmark": str(benchmark_path.resolve()),
        "review_csv": str(review_path.resolve()),
        "review_csv_sha256": _sha256(review_path.read_bytes()),
        "benchmark_before_sha256": _sha256(benchmark_before),
        "benchmark_after_sha256": _sha256(benchmark_after),
        "projected_benchmark_sha256": _sha256(projected_benchmark),
        "change_count": len(changes),
        "already_applied": already_applied,
        "changes": changes,
    }
    previous_ledger = ledger_path.read_bytes() if ledger_path.exists() else b""
    ledger_line = (
        json.dumps(ledger_record, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(ledger_path, previous_ledger + ledger_line)
    return ledger_record


def main():
    parser = argparse.ArgumentParser(
        description="Validate a human benchmark review; dry-run is the default."
    )
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("review_csv", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="Atomically update the benchmark after all rows validate.",
    )
    args = parser.parse_args()
    try:
        result = import_review(
            args.benchmark, args.review_csv, args.ledger, apply=args.apply,
        )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
