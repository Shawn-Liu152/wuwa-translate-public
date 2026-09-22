#!/usr/bin/env python3
"""Build a leakage-checked, explicitly unreviewed benchmark candidate pack."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _load_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {source}")
    return payload


def _normalized_sources(entries, language: str) -> set[str]:
    from pipeline.translation_memory import normalize_source

    normalized = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = (
            entry.get("canonical_source_text")
            or entry.get("source")
            or entry.get("original")
            or ""
        )
        value = normalize_source(str(source), source_language=language)
        if value:
            normalized.add(value)
    return normalized


def build_candidate_pack(
    job_dir: str | Path,
    language: str,
    benchmark_path: str | Path,
    translation_memory_path: str | Path,
    *,
    output_json: str | Path | None = None,
    output_csv: str | Path | None = None,
) -> dict[str, Any]:
    """Extract machine translations without claiming human confirmation."""
    from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms
    from pipeline.translation_memory import normalize_source

    if language not in {"en", "ja", "ko"}:
        raise ValueError(f"unsupported source language: {language}")
    job = Path(job_dir).resolve()
    union_path = job / f"final.zh.{language}.union.final.srt"
    round2_path = job / "final.round2.zh.srt"
    if not union_path.is_file() or not round2_path.is_file():
        raise FileNotFoundError(
            f"job requires {union_path.name} and {round2_path.name}"
        )

    benchmark = _load_object(benchmark_path)
    benchmark_language = str(benchmark.get("source_language") or "")
    if benchmark_language != language:
        raise ValueError(
            f"benchmark language is {benchmark_language}, not {language}"
        )
    translation_memory = _load_object(translation_memory_path)
    benchmark_sources = _normalized_sources(
        benchmark.get("entries") or [], language,
    )
    approved_memory = [
        entry for entry in (translation_memory.get("entries") or [])
        if isinstance(entry, dict)
        and entry.get("approved") is True
        and entry.get("source_language") == language
    ]
    memory_sources = _normalized_sources(approved_memory, language)

    source_by_id = {subtitle.id: subtitle for subtitle in parse_srt(str(union_path))}
    target_by_id = {subtitle.id: subtitle for subtitle in parse_srt(str(round2_path))}
    round1_path = job / "final.round1.zh.srt"
    round1_by_id = (
        {subtitle.id: subtitle for subtitle in parse_srt(str(round1_path))}
        if round1_path.is_file()
        else target_by_id
    )
    risk_path = job / "final.zh.risk.generated.json"
    risk_payload = _load_object(risk_path) if risk_path.is_file() else {}
    reasons_by_id: dict[int, set[str]] = {}
    risk_keys_by_id: dict[int, list[str]] = {}
    for item in risk_payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            subtitle_id = int(item.get("subtitle_id"))
        except (TypeError, ValueError):
            continue
        reasons_by_id.setdefault(subtitle_id, set()).update(
            str(reason) for reason in (item.get("reasons") or []) if reason
        )
        risk_key = str(item.get("key") or "").strip()
        if risk_key:
            risk_keys_by_id.setdefault(subtitle_id, []).append(risk_key)

    round2_results_path = job / "final.zh.round2.results.json"
    round2_results_payload = (
        _load_object(round2_results_path)
        if round2_results_path.is_file()
        else {}
    )
    round2_results_by_key = {
        str(item.get("key")): item
        for item in (round2_results_payload.get("items") or [])
        if isinstance(item, dict) and item.get("key")
    }

    summary_path = job / "holdout_summary.json"
    summary = _load_object(summary_path) if summary_path.is_file() else {}
    video_id = str(summary.get("video_id") or job.name)
    benchmark_overlap = 0
    tm_overlap = 0
    missing_pair = 0
    entries = []
    for subtitle_id in sorted(set(source_by_id) | set(target_by_id)):
        source = source_by_id.get(subtitle_id)
        target = target_by_id.get(subtitle_id)
        if not source or not target or not source.text.strip() or not target.text.strip():
            missing_pair += 1
            continue
        normalized = normalize_source(source.text, source_language=language)
        if normalized in benchmark_sources:
            benchmark_overlap += 1
            continue
        if normalized in memory_sources:
            tm_overlap += 1
            continue
        stable_key = f"{source.start}|{source.end}|{subtitle_id}"
        round1 = round1_by_id.get(subtitle_id)
        round1_text = round1.text if round1 else target.text
        round2_reviews = []
        for risk_key in risk_keys_by_id.get(subtitle_id, []):
            review = round2_results_by_key.get(risk_key)
            if not review:
                continue
            round2_reviews.append({
                "key": risk_key,
                "decision": review.get("decision"),
                "accepted": review.get("accepted"),
                "applied": review.get("applied"),
                "round2_status": review.get("round2_status"),
                "state": review.get("state"),
                "confidence": review.get("confidence"),
                "validation_reasons": list(
                    review.get("validation_reasons") or []
                ),
            })
        entries.append({
            "id": f"{video_id}:{stable_key}",
            "start_ms": timestamp_to_ms(source.start),
            "end_ms": timestamp_to_ms(source.end),
            "youtube_text": source.text,
            "whisper_text": "",
            "canonical_source_text": source.text,
            "gold_zh": target.text,
            "round1_zh": round1_text,
            "round2_zh": target.text,
            "machine_changed": round1_text.strip() != target.text.strip(),
            "round2_reviews": round2_reviews,
            "gold_status": "provisional",
            "candidate_status": "machine_generated_unreviewed",
            "human_reviewed": False,
            "review_required": True,
            "entities": [],
            "error_tags": sorted(reasons_by_id.get(subtitle_id, set())),
            "provenance": {
                "job_id": job.name,
                "video_id": video_id,
                "source_artifact": union_path.name,
                "translation_artifact": round2_path.name,
            },
        })

    payload = {
        "schema_version": 1,
        "name": f"{language}-reaction-machine-candidates-{video_id}",
        "description": (
            "Machine-generated, leakage-checked candidates for genuine human "
            "review. No entry is human-confirmed."
        ),
        "source_language": language,
        "target_language": "zh-CN",
        "gold_policy": "provisional_machine_candidate_only",
        "human_reviewed": False,
        "source_job": job.name,
        "video_id": video_id,
        "leakage_audit": {
            "benchmark_overlap_skipped": benchmark_overlap,
            "approved_tm_overlap_skipped": tm_overlap,
            "missing_pair_skipped": missing_pair,
        },
        "entries": entries,
    }
    if output_csv is not None and output_json is None:
        raise ValueError("output_json is required when output_csv is requested")
    if output_json is not None:
        destination = Path(output_json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if output_csv is not None:
            from tools.export_benchmark_review import export_review

            export_review(destination, output_csv, gold_status="provisional")

    return {
        "job": str(job),
        "language": language,
        "candidate_count": len(entries),
        "benchmark_overlap_skipped": benchmark_overlap,
        "tm_overlap_skipped": tm_overlap,
        "missing_pair_skipped": missing_pair,
        "output_json": str(Path(output_json).resolve()) if output_json else None,
        "output_csv": str(Path(output_csv).resolve()) if output_csv else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build an unreviewed benchmark candidate JSON and blank human-review CSV."
        )
    )
    parser.add_argument("language", choices=["en", "ja", "ko"])
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument(
        "--translation-memory", type=Path,
        default=BASE / "data" / "translation_memory.json",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()
    result = build_candidate_pack(
        args.job_dir,
        args.language,
        args.benchmark,
        args.translation_memory,
        output_json=args.output_json,
        output_csv=args.output_csv,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
