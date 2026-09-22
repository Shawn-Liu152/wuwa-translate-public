#!/usr/bin/env python3
"""Prepare a deterministic, leakage-checked, genuinely unreviewed batch."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

REVIEW_BATCH_FIELDS = [
    "language",
    "id",
    "review_stratum",
    "canonical_source_text",
    "round1_zh",
    "round2_zh",
    "machine_changed",
    "error_tags",
    "round2_reviews",
    "review_decision",
    "final_zh",
    "source_quality",
    "review_notes",
    "evidence",
    "reviewer",
    "reviewed_at",
]

STRATUM_ORDER = (
    "entity",
    "source_truth",
    "residue",
    "semantic",
    "person_name",
    "other_risk",
    "clean_control",
)

_ENTITY_REASONS = {
    "contextual_entity_preserved", "contextual_entity_unverified",
    "hallucinated_entity", "suspected_name", "unknown_entity",
}
_SOURCE_REASONS = {"source_unresolved", "text_conflict"}
_RESIDUE_REASONS = {"english_residue", "hangul_residue", "kana_residue"}
_SEMANTIC_REASONS = {
    "condition_marker_missing", "empty_translation", "incomplete_fragment",
    "negation_missing", "number_mismatch", "semantic_marker_missing",
    "term_mismatch",
}


def _load_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {source}")
    return payload


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _normalised_sources(entries, language: str) -> set[str]:
    from pipeline.translation_memory import normalize_source

    result = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = (
            entry.get("canonical_source_text")
            or entry.get("source")
            or entry.get("original")
            or ""
        )
        normalized = normalize_source(str(source), source_language=language)
        if normalized:
            result.add(normalized)
    return result


def _reason_prefixes(entry: dict[str, Any]) -> set[str]:
    return {
        str(reason).split(":", 1)[0]
        for reason in (entry.get("error_tags") or [])
        if str(reason).strip()
    }


def classify_review_stratum(entry: dict[str, Any]) -> str:
    """Return the one primary review stratum used for deterministic sampling."""
    reasons = _reason_prefixes(entry)
    if reasons.intersection(_ENTITY_REASONS):
        return "entity"
    if reasons.intersection(_SOURCE_REASONS):
        return "source_truth"
    if reasons.intersection(_RESIDUE_REASONS):
        return "residue"
    if reasons.intersection(_SEMANTIC_REASONS):
        return "semantic"
    if "person_name_present" in reasons:
        return "person_name"
    if reasons:
        return "other_risk"
    return "clean_control"


def _stable_entry_key(entry: dict[str, Any]) -> tuple[int, str]:
    entry_id = str(entry.get("id") or "")
    digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
    return (0 if entry.get("machine_changed") is True else 1, digest)


def _parse_language_paths(values: list[str], option: str) -> dict[str, Path]:
    result = {}
    for value in values:
        language, separator, raw_path = value.partition("=")
        if not separator or language not in {"en", "ja", "ko"} or not raw_path:
            raise ValueError(f"{option} must use LANG=PATH for en/ja/ko")
        if language in result:
            raise ValueError(f"duplicate {option} language: {language}")
        result[language] = Path(raw_path)
    return result


def prepare_review_batch(
    candidate_paths: dict[str, str | Path],
    benchmark_paths: dict[str, str | Path],
    translation_memory_path: str | Path,
    *,
    per_language: int,
    output_json: str | Path,
    output_csv: str | Path,
    output_report: str | Path,
) -> dict[str, Any]:
    """Select a balanced batch without creating any human-labelled state."""
    if per_language < 1:
        raise ValueError("per_language must be positive")
    if set(candidate_paths) != set(benchmark_paths):
        raise ValueError("candidate and benchmark languages must match")

    from pipeline.translation_memory import normalize_source

    memory = _load_object(translation_memory_path)
    memory_by_language = {}
    for language in candidate_paths:
        approved = [
            entry for entry in (memory.get("entries") or [])
            if isinstance(entry, dict)
            and entry.get("approved") is True
            and entry.get("source_language") == language
        ]
        memory_by_language[language] = _normalised_sources(approved, language)

    selected = []
    selected_by_language = {}
    selected_by_stratum: Counter[str] = Counter()
    leakage_skipped: Counter[str] = Counter({
        "benchmark": 0,
        "approved_tm": 0,
        "duplicate_source": 0,
    })
    input_hashes = {"translation_memory": _sha256(translation_memory_path)}

    for language in sorted(candidate_paths):
        pack_path = Path(candidate_paths[language])
        benchmark_path = Path(benchmark_paths[language])
        pack = _load_object(pack_path)
        benchmark = _load_object(benchmark_path)
        if pack.get("source_language") != language:
            raise ValueError(f"candidate language mismatch: {pack_path}")
        if benchmark.get("source_language") != language:
            raise ValueError(f"benchmark language mismatch: {benchmark_path}")
        if pack.get("human_reviewed") is not False:
            raise ValueError(f"candidate pack must be explicitly unreviewed: {pack_path}")

        input_hashes[f"candidate_{language}"] = _sha256(pack_path)
        input_hashes[f"benchmark_{language}"] = _sha256(benchmark_path)
        benchmark_sources = _normalised_sources(
            benchmark.get("entries") or [], language,
        )
        seen_sources = set()
        seen_ids = set()
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw_entry in pack.get("entries") or []:
            if not isinstance(raw_entry, dict):
                continue
            entry_id = str(raw_entry.get("id") or "").strip()
            if not entry_id:
                raise ValueError(f"candidate entry id is required: {pack_path}")
            if entry_id in seen_ids:
                raise ValueError(f"duplicate candidate id: {entry_id}")
            seen_ids.add(entry_id)
            if (
                raw_entry.get("human_reviewed") is not False
                or raw_entry.get("candidate_status")
                != "machine_generated_unreviewed"
                or raw_entry.get("gold_status") != "provisional"
            ):
                raise ValueError(f"{entry_id}: reviewed/confirmed candidate contamination")
            source = str(raw_entry.get("canonical_source_text") or "")
            normalized = normalize_source(source, source_language=language)
            if not normalized:
                continue
            if normalized in benchmark_sources:
                leakage_skipped["benchmark"] += 1
                continue
            if normalized in memory_by_language[language]:
                leakage_skipped["approved_tm"] += 1
                continue
            if normalized in seen_sources:
                leakage_skipped["duplicate_source"] += 1
                continue
            seen_sources.add(normalized)
            entry = dict(raw_entry)
            entry["language"] = language
            entry["review_stratum"] = classify_review_stratum(entry)
            buckets[entry["review_stratum"]].append(entry)

        for entries in buckets.values():
            entries.sort(key=_stable_entry_key)
        language_selection = []
        while len(language_selection) < per_language:
            progressed = False
            for stratum in STRATUM_ORDER:
                if buckets[stratum] and len(language_selection) < per_language:
                    language_selection.append(buckets[stratum].pop(0))
                    progressed = True
            if not progressed:
                break
        for rank, entry in enumerate(language_selection, start=1):
            entry["selection_rank"] = rank
            entry["human_reviewed"] = False
            selected_by_stratum[entry["review_stratum"]] += 1
        selected.extend(language_selection)
        selected_by_language[language] = len(language_selection)

    payload = {
        "schema_version": 1,
        "kind": "stratified_human_review_batch",
        "review_status": "awaiting_genuine_human_review",
        "human_reviewed": False,
        "per_language_requested": per_language,
        "selected_by_language": selected_by_language,
        "selected_by_stratum": dict(sorted(selected_by_stratum.items())),
        "leakage_skipped": dict(leakage_skipped),
        "input_sha256": input_hashes,
        "review_decisions": [
            "round1_preferred", "round2_preferred", "both_correct",
            "both_wrong", "source_error", "skip",
        ],
        "entries": selected,
    }

    json_path = Path(output_json)
    csv_path = Path(output_csv)
    report_path = Path(output_report)
    for destination in (json_path, csv_path, report_path):
        destination.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_BATCH_FIELDS)
        writer.writeheader()
        for entry in selected:
            writer.writerow({
                "language": entry["language"],
                "id": entry["id"],
                "review_stratum": entry["review_stratum"],
                "canonical_source_text": entry.get("canonical_source_text", ""),
                "round1_zh": entry.get("round1_zh", entry.get("gold_zh", "")),
                "round2_zh": entry.get("round2_zh", entry.get("gold_zh", "")),
                "machine_changed": str(bool(entry.get("machine_changed"))).lower(),
                "error_tags": json.dumps(
                    entry.get("error_tags") or [], ensure_ascii=False,
                ),
                "round2_reviews": json.dumps(
                    entry.get("round2_reviews") or [], ensure_ascii=False,
                ),
                "review_decision": "",
                "final_zh": "",
                "source_quality": "",
                "review_notes": "",
                "evidence": "",
                "reviewer": "",
                "reviewed_at": "",
            })

    lines = [
        "# Stratified Human Review Batch",
        "",
        "> No human correctness metric is observable yet. Every review field "
        "is blank and every entry remains machine-generated/unreviewed.",
        "",
        "| Language | Selected |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {language} | {count} |"
        for language, count in sorted(selected_by_language.items())
    )
    lines += ["", "## Strata", "", "| Stratum | Selected |", "|---|---:|"]
    lines.extend(
        f"| {stratum} | {count} |"
        for stratum, count in sorted(selected_by_stratum.items())
    )
    lines += [
        "",
        "## Leakage re-check",
        "",
        f"- Existing benchmark overlaps skipped: {leakage_skipped['benchmark']}",
        f"- Approved TM overlaps skipped: {leakage_skipped['approved_tm']}",
        f"- Duplicate candidate sources skipped: {leakage_skipped['duplicate_source']}",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "selected_by_language": selected_by_language,
        "selected_by_stratum": dict(sorted(selected_by_stratum.items())),
        "leakage_skipped": dict(leakage_skipped),
        "output_json": str(json_path.resolve()),
        "output_csv": str(csv_path.resolve()),
        "output_report": str(report_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a blank, stratified human-review batch.",
    )
    parser.add_argument(
        "--candidate", action="append", required=True, metavar="LANG=PATH",
    )
    parser.add_argument(
        "--benchmark", action="append", required=True, metavar="LANG=PATH",
    )
    parser.add_argument(
        "--translation-memory", type=Path,
        default=BASE / "data" / "translation_memory.json",
    )
    parser.add_argument("--per-language", type=int, default=20)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        candidates = _parse_language_paths(args.candidate, "--candidate")
        benchmarks = _parse_language_paths(args.benchmark, "--benchmark")
        result = prepare_review_batch(
            candidates,
            benchmarks,
            args.translation_memory,
            per_language=args.per_language,
            output_json=args.output_json,
            output_csv=args.output_csv,
            output_report=args.output_report,
        )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
