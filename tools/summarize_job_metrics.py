#!/usr/bin/env python3
"""Build read-only, reproducible metrics summaries for subtitle jobs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

FIELDNAMES = [
    "job_id", "source_language", "round1_model", "round2_model",
    "source_units", "union_units", "source_unresolved_count",
    "source_unresolved_rate", "review_required_count",
    "review_required_rate", "glossary_hit_units", "glossary_hit_rate",
    "glossary_match_occurrences", "average_glossary_terms",
    "max_glossary_terms", "glossary_metrics_basis",
    "glossary_reference_sha256", "round1_tokens_in", "round1_tokens_out",
    "round1_wall_seconds", "round2_eligible", "keep",
    "accepted_replace", "replace_rejected", "unresolved", "no_result",
    "accepted_replace_rate", "round2_no_result_rate",
    "effective_fix_rate", "effective_fix_rate_observation",
    "round2_tokens_in", "round2_tokens_out", "round2_wall_seconds",
    "total_tokens", "total_wall_seconds", "estimated_cost",
    "estimated_cost_currency", "cost_availability", "display_errors",
    "display_warnings", "source_residue", "critical_error_count",
]


def _artifact(job_dir: Path, name: str) -> Path | None:
    """Return a stable artifact path without changing the job directory."""
    direct = job_dir / name
    if direct.is_file():
        return direct
    process = job_dir / "过程文件" / name
    if process.is_file():
        return process
    matches = sorted(
        path for path in job_dir.rglob(name)
        if path.is_file()
    )
    return matches[0] if matches else None


def resolve_job_paths(
    jobs: Iterable[str | Path], download_root: str | Path,
) -> list[Path]:
    """Resolve explicit paths/IDs, or discover artifact-bearing job folders."""
    root = Path(download_root).resolve()
    requested = list(jobs)
    if requested:
        resolved: list[Path] = []
        for value in requested:
            candidate = Path(value)
            if not candidate.is_absolute() and not candidate.is_dir():
                candidate = root / candidate
            candidate = candidate.resolve()
            if not candidate.is_dir():
                raise FileNotFoundError(f"job directory not found: {value}")
            resolved.append(candidate)
        return resolved
    if not root.is_dir():
        return []
    markers = (
        "final.zh.pipeline.metrics.json",
        "final.round1.zh.srt.manifest.json",
    )
    return sorted(
        (child.resolve() for child in root.iterdir()
         if child.is_dir() and any(_artifact(child, marker) for marker in markers)),
        key=lambda path: path.name,
    )


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_term(text: str, term: str, source_language: str) -> int:
    normalized_text = unicodedata.normalize("NFKC", text)
    normalized_term = unicodedata.normalize("NFKC", term)
    if not normalized_term:
        return 0
    if source_language == "ja":
        return normalized_text.count(normalized_term)
    if source_language == "ko":
        return len(re.findall(
            rf"(?<![\uac00-\ud7af]){re.escape(normalized_term)}",
            normalized_text,
        ))
    return len(re.findall(
        rf"(?<![A-Za-z0-9]){re.escape(normalized_term)}(?![A-Za-z0-9])",
        normalized_text,
        re.IGNORECASE,
    ))


def _backfill_glossary_usage(
    entries: list[Any], glossary_db: Path, source_language: str,
) -> dict[str, Any] | None:
    """Read current glossary terms without seeding or mutating the database."""
    if not glossary_db.is_file() or not entries:
        return None
    uri = f"file:{glossary_db.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "SELECT DISTINCT source_term FROM glossary "
            "WHERE game=? AND source_language=? AND source_term<>''",
            ("wuwa", source_language),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()
    terms = sorted((str(row[0]) for row in rows), key=len, reverse=True)
    selected_terms: list[str] = []
    for term in terms:
        if source_language in {"ja", "ko"} and any(
            term in existing for existing in selected_terms
        ):
            continue
        selected_terms.append(term)
    hit_units = 0
    occurrences = 0
    translatable_units = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("original") or "")
        translatable_units += 1
        unit_occurrences = sum(
            _count_term(text, term, source_language) for term in selected_terms
        )
        if unit_occurrences:
            hit_units += 1
            occurrences += unit_occurrences
    return {
        "translatable_units": translatable_units,
        "glossary_hit_units": hit_units,
        "glossary_match_occurrences": occurrences,
        "glossary_metrics_basis": "current_glossary_read_only_backfill",
        "glossary_reference_sha256": hashlib.sha256(
            glossary_db.read_bytes()
        ).hexdigest(),
    }


def _number(value: Any, default: int | float = 0) -> int | float:
    if isinstance(value, bool):
        return default
    return value if isinstance(value, (int, float)) else default


def _rate(count: Any, total: Any) -> float | None:
    count_value = _number(count)
    total_value = _number(total)
    if total_value <= 0:
        return None
    return round(float(count_value) / float(total_value), 6)


def _effective_wall_seconds(
    metrics: dict[str, Any], manifest: dict[str, Any],
) -> float:
    """Preserve model time when a resume records only restore overhead.

    A resumed run can rewrite ``wall_seconds`` to a few milliseconds while
    immutable batch records retain their original request durations.  The
    longest batch is a safe lower bound for concurrent work; a larger genuine
    top-level wall measurement remains preferred.
    """
    reported = max(
        float(_number(metrics.get("wall_seconds"), 0.0)),
        float(_number(
            (manifest.get("metrics") or {}).get("wall_seconds"), 0.0,
        )),
    )
    batch_elapsed = [
        float(_number(
            (batch.get("metrics") or {}).get("elapsed_seconds"), 0.0,
        ))
        for batch in (manifest.get("batches") or {}).values()
        if isinstance(batch, dict)
    ]
    return max([reported, *batch_elapsed], default=reported)


def _reason_matches(item: dict[str, Any], expected: str) -> bool:
    return any(
        str(reason) == expected or str(reason).startswith(expected + ":")
        for reason in (item.get("reasons") or [])
    )


def _infer_round2_summary(results: dict[str, Any]) -> dict[str, int]:
    existing = (results.get("metrics") or {}).get("round2_summary")
    if isinstance(existing, dict):
        return {
            key: int(_number(existing.get(key), 0))
            for key in (
                "round2_eligible", "keep", "replace", "replace_rejected",
                "unresolved", "no_result", "deferred_weak_risk",
            )
        }
    counts = {
        "round2_eligible": 0,
        "keep": 0,
        "replace": 0,
        "replace_rejected": 0,
        "unresolved": 0,
        "no_result": 0,
        "deferred_weak_risk": 0,
    }
    items = results.get("items") or []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("round2_status") or "").strip().lower()
        if not status:
            decision = str(item.get("decision") or "").strip().lower()
            if decision == "keep":
                status = "keep"
            elif decision == "replace":
                status = "replace" if item.get("accepted") else "replace_rejected"
            elif item.get("state") == "on_demand":
                status = "deferred_weak_risk"
            else:
                status = "no_result"
        if status in counts:
            counts[status] += 1
    counts["round2_eligible"] = sum(
        counts[key]
        for key in ("keep", "replace", "replace_rejected", "unresolved", "no_result")
    )
    return counts


def _estimate_cost(
    round1_model: str,
    round2_model: str,
    round1: dict[str, Any],
    round2: dict[str, Any],
) -> tuple[float | None, str | None, str]:
    from pipeline.pricing import estimate_model_cost

    amounts: list[float] = []
    currencies: set[str] = set()
    for model, metrics in ((round1_model, round1), (round2_model, round2)):
        tokens_in = int(_number(metrics.get("tokens_in"), 0))
        tokens_out = int(_number(metrics.get("tokens_out"), 0))
        if not (tokens_in or tokens_out):
            continue
        estimate = estimate_model_cost(model, tokens_in, tokens_out)
        if not estimate.get("estimable"):
            return None, None, "unavailable"
        amounts.append(float(estimate["amount"]))
        currencies.add(str(estimate["currency"]))
    if not amounts or len(currencies) != 1:
        return None, None, "unavailable"
    return round(sum(amounts), 8), currencies.pop(), "estimated"


def summarize_job(
    job_dir: str | Path, *, glossary_db: str | Path | None = None,
) -> dict[str, Any]:
    """Summarize one job using artifacts only; never write into ``job_dir``."""
    job = Path(job_dir).resolve()
    pipeline_metrics = _load_json(_artifact(job, "final.zh.pipeline.metrics.json"))
    round1_manifest = _load_json(_artifact(job, "final.round1.zh.srt.manifest.json"))
    round2_manifest = _load_json(_artifact(job, "final.round2.zh.srt.manifest.json"))
    round2_results = _load_json(_artifact(job, "final.zh.round2.results.json"))
    risk_payload = _load_json(_artifact(job, "final.zh.risk.generated.json"))
    final_audit = _load_json(_artifact(job, "blind_audit.json"))

    round1 = dict(pipeline_metrics.get("round1") or round1_manifest.get("metrics") or {})
    round2 = dict(pipeline_metrics.get("round2") or round2_manifest.get("metrics") or {})
    quality = dict(pipeline_metrics.get("quality") or {})
    round1_options = dict(round1_manifest.get("options") or {})
    round2_options = dict(round2_manifest.get("options") or {})
    source_language = str(
        round1_options.get("source_language")
        or round2_options.get("source_language")
        or "unknown"
    )
    round1_model = str(round1_options.get("model") or "unknown")
    round2_model = str(round2_options.get("model") or round1_model)
    if not final_audit and source_language in {"en", "ja", "ko"}:
        final_srt = _artifact(job, "final.zh.srt")
        if final_srt:
            try:
                from tools.audit_blind_video import audit
                final_audit = audit(source_language, final_srt)
            except (OSError, UnicodeError, ValueError):
                final_audit = {}

    glossary_basis = "runtime_manifest" if (
        "glossary_hit_units" in round1
        and "glossary_match_occurrences" in round1
    ) else "unavailable"
    glossary_reference_sha256 = None
    if glossary_basis == "unavailable":
        backfill = _backfill_glossary_usage(
            list(round1_manifest.get("entries") or []),
            Path(glossary_db or BASE / "data" / "glossary.db"),
            source_language,
        )
        if backfill:
            round1.update(backfill)
            glossary_basis = str(backfill["glossary_metrics_basis"])
            glossary_reference_sha256 = str(
                backfill["glossary_reference_sha256"]
            )

    risk_items = risk_payload.get("items") or []
    if not isinstance(risk_items, list):
        risk_items = []
    source_units = int(_number(quality.get("primary_subtitles"), 0))
    union_units = int(
        _number(quality.get("union_subtitles"), len(round1_manifest.get("entries") or []))
    )
    source_unresolved_count = sum(
        1 for item in risk_items
        if isinstance(item, dict) and _reason_matches(item, "source_unresolved")
    )
    review_required_count = int(_number(
        quality.get("review_required_count"),
        sum(
            bool(item.get("review_required"))
            for item in risk_items if isinstance(item, dict)
        ),
    ))

    translatable_units = int(_number(
        round1.get("translatable_units"), len(round1_manifest.get("entries") or [])
    ))
    glossary_hit_units_value = round1.get("glossary_hit_units")
    glossary_hit_units = (
        int(_number(glossary_hit_units_value))
        if glossary_hit_units_value is not None else None
    )
    glossary_occurrences_value = round1.get("glossary_match_occurrences")
    glossary_match_occurrences = (
        int(_number(glossary_occurrences_value))
        if glossary_occurrences_value is not None else None
    )

    r2 = _infer_round2_summary(round2_results)
    estimated_cost, cost_currency, cost_availability = _estimate_cost(
        round1_model, round2_model, round1, round2,
    )
    round1_tokens_in = int(_number(round1.get("tokens_in"), 0))
    round1_tokens_out = int(_number(round1.get("tokens_out"), 0))
    round2_tokens_in = int(_number(round2.get("tokens_in"), 0))
    round2_tokens_out = int(_number(round2.get("tokens_out"), 0))
    round1_wall = _effective_wall_seconds(round1, round1_manifest)
    round2_wall = _effective_wall_seconds(round2, round2_manifest)
    source_residue = (
        int(_number(final_audit.get("source_residual"), 0))
        if "source_residual" in final_audit else None
    )
    audit_critical = (
        int(_number(final_audit.get("overlap"), 0))
        + int(_number(final_audit.get("nonpositive_duration"), 0))
        + int(_number(final_audit.get("source_residual"), 0))
        + len(final_audit.get("split_version_numbers") or [])
    )
    display_errors = int(_number(quality.get("display_errors"), 0))

    return {
        "job_id": job.name,
        "source_language": source_language,
        "round1_model": round1_model,
        "round2_model": round2_model,
        "source_units": source_units,
        "union_units": union_units,
        "source_unresolved_count": source_unresolved_count,
        "source_unresolved_rate": _rate(source_unresolved_count, union_units),
        "review_required_count": review_required_count,
        "review_required_rate": _rate(review_required_count, union_units),
        "glossary_hit_units": glossary_hit_units,
        "glossary_hit_rate": _rate(glossary_hit_units, translatable_units),
        "glossary_match_occurrences": glossary_match_occurrences,
        "average_glossary_terms": _number(round1.get("average_glossary_terms"), 0.0),
        "max_glossary_terms": int(_number(round1.get("max_glossary_terms"), 0)),
        "glossary_metrics_basis": glossary_basis,
        "glossary_reference_sha256": glossary_reference_sha256,
        "round1_tokens_in": round1_tokens_in,
        "round1_tokens_out": round1_tokens_out,
        "round1_wall_seconds": round(round1_wall, 3),
        "round2_eligible": r2["round2_eligible"],
        "keep": r2["keep"],
        "accepted_replace": r2["replace"],
        "replace_rejected": r2["replace_rejected"],
        "unresolved": r2["unresolved"],
        "no_result": r2["no_result"],
        "accepted_replace_rate": _rate(r2["replace"], r2["round2_eligible"]),
        "round2_no_result_rate": _rate(r2["no_result"], r2["round2_eligible"]),
        "effective_fix_rate": None,
        "effective_fix_rate_observation": "not_observable",
        "round2_tokens_in": round2_tokens_in,
        "round2_tokens_out": round2_tokens_out,
        "round2_wall_seconds": round(round2_wall, 3),
        "total_tokens": (
            round1_tokens_in + round1_tokens_out
            + round2_tokens_in + round2_tokens_out
        ),
        "total_wall_seconds": round(round1_wall + round2_wall, 3),
        "estimated_cost": estimated_cost,
        "estimated_cost_currency": cost_currency,
        "cost_availability": cost_availability,
        "display_errors": display_errors,
        "display_warnings": int(_number(quality.get("display_warnings"), 0)),
        "source_residue": source_residue,
        "critical_error_count": max(display_errors, audit_critical),
    }


def write_reports(
    records: Iterable[dict[str, Any]], output_dir: str | Path,
) -> dict[str, Path]:
    """Write the stable JSONL, CSV and Markdown report trio."""
    rows = list(records)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "jsonl": destination / "job_metrics.jsonl",
        "csv": destination / "job_metrics.csv",
        "markdown": destination / "JOB_METRICS_SUMMARY.md",
    }
    paths["jsonl"].write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    with paths["csv"].open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in FIELDNAMES})

    lines = [
        "# Job Metrics Summary",
        "",
        "Generated from immutable job artifacts by `tools/summarize_job_metrics.py`.",
        "",
        "> accepted_replace is a deterministic gate outcome, not a claim that the edit is correct. "
        "Without human labels, `effective_fix_rate` is `null` and its observation status is "
        "`not_observable`. Unknown prices are reported as unavailable, never as zero cost.",
        "",
        "| Job | Lang | Source | Union | Source unresolved | Review required | R2 eligible | "
        "Accepted replace | No result | Tokens | Wall s | Cost | Critical |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for row in rows:
        cost = (
            f"{row.get('estimated_cost')} {row.get('estimated_cost_currency')}"
            if row.get("cost_availability") == "estimated" else "unavailable"
        )
        lines.append(
            "| {job_id} | {source_language} | {source_units} | {union_units} | "
            "{source_unresolved_count} ({source_unresolved_rate}) | "
            "{review_required_count} ({review_required_rate}) | {round2_eligible} | "
            "{accepted_replace} ({accepted_replace_rate}) | {no_result} "
            "({round2_no_result_rate}) | {total_tokens} | {total_wall_seconds} | "
            "{cost} | {critical_error_count} |".format(
                cost=cost,
                **{key: row.get(key) for key in FIELDNAMES},
            )
        )
    lines += [
        "",
        "## Metric semantics",
        "",
        "- `source_unresolved_rate`, `review_required_rate`, and "
        "`round2_no_result_rate` use different denominators and are never merged.",
        "- `glossary_hit_rate` is units with at least one glossary hit divided by "
        "translatable units; legacy backfills are explicitly identified in JSONL/CSV.",
        "- Estimated cost is informational and uses a documented model price; relay billing "
        "can differ.",
        "",
    ]
    paths["markdown"].write_text("\n".join(lines), encoding="utf-8")
    return paths


def main(argv: Iterable[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="*", help="job paths or IDs")
    parser.add_argument("--download-root", type=Path, default=BASE / "download")
    parser.add_argument("--output-dir", type=Path, default=BASE / "reports")
    args = parser.parse_args(list(argv) if argv is not None else None)
    records = [
        summarize_job(path)
        for path in resolve_job_paths(args.jobs, args.download_root)
    ]
    paths = write_reports(records, args.output_dir)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
