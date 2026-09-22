# -*- coding: utf-8 -*-
"""Read-only Translation Memory, holdout-leakage, and few-shot audit."""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline.translation_memory import normalize_source
from tools.run_benchmark import audit_tm_leakage


def _script_mismatch(source, language):
    has_hangul = bool(re.search(r"[\uac00-\ud7af]", source))
    has_kana = bool(re.search(r"[\u3040-\u30ff]", source))
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", source))
    if language == "en":
        return has_hangul or has_kana or has_cjk
    if language == "ja":
        return has_hangul
    if language == "ko":
        return has_kana
    return True


def _contains_term(source, term, language):
    if not term:
        return False
    if language == "en":
        pattern = re.escape(term).replace(r"\ ", r"\s+")
        return bool(re.search(
            rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])",
            source, re.IGNORECASE,
        ))
    return term in source


def audit_translation_memory(
    memory, *, holdouts=None, evidence=None, max_source_chars=160,
):
    """Return an objective audit and review queue without mutating ``memory``."""
    entries = list(memory.get("entries") or [])
    candidates = {}

    def flag(entry, reason, *, holdout_id=None):
        entry_id = str(entry.get("id") or "<missing-id>")
        item = candidates.setdefault(entry_id, {
            "tm_id": entry_id,
            "language": str(entry.get("source_language") or ""),
            "approved": entry.get("approved") is True,
            "source": str(entry.get("source") or ""),
            "final": str(entry.get("final") or ""),
            "status": "NEEDS_HUMAN_REVIEW",
            "reasons": [],
            "holdout_ids": [],
        })
        if reason not in item["reasons"]:
            item["reasons"].append(reason)
        if holdout_id and holdout_id not in item["holdout_ids"]:
            item["holdout_ids"].append(holdout_id)

    approved_by_language = Counter()
    unapproved_by_language = Counter()
    normalized_groups = defaultdict(list)
    pollution = []
    damaged = []
    overlong = []
    transcript_artifacts = []
    malformed = []
    stale_terms = []
    replacement_actions = [
        action for action in (evidence or {}).get("glossary_actions", [])
        if action.get("operation") == "replace"
    ]

    for entry in entries:
        language = str(entry.get("source_language") or "")
        source = str(entry.get("source") or "")
        final = str(entry.get("final") or "")
        entry_id = str(entry.get("id") or "<missing-id>")
        if entry.get("approved") is True:
            approved_by_language[language] += 1
        else:
            unapproved_by_language[language] += 1
            flag(entry, "awaiting_human_approval")
        missing = [
            field for field, value in (
                ("id", entry.get("id")),
                ("source", source),
                ("final", final),
                ("source_language", language),
                ("target_language", entry.get("target_language")),
            )
            if not value
        ]
        if missing:
            malformed.append({"tm_id": entry_id, "missing": missing})
            flag(entry, "missing_required_field")
        if source and language:
            normalized = normalize_source(source, source_language=language)
            normalized_groups[(language, normalized)].append(entry)
        if source and _script_mismatch(source, language):
            pollution.append(entry_id)
            flag(entry, "cross_language_script")
        if "\ufffd" in source or "\x00" in source:
            damaged.append(entry_id)
            flag(entry, "source_damaged")
        if len(source) > max_source_chars:
            overlong.append({
                "tm_id": entry_id,
                "source_chars": len(source),
                "limit": max_source_chars,
            })
            flag(entry, "overlong_example")
        if ">>" in source:
            transcript_artifacts.append(entry_id)
            flag(entry, "transcript_artifact")
        for action in replacement_actions:
            if language == action.get("source_language") and _contains_term(
                source, str(action.get("old_source_term") or ""), language,
            ):
                stale_terms.append({
                    "tm_id": entry_id,
                    "old_term": action["old_source_term"],
                    "official_term": action.get("source_term"),
                    "action_id": action.get("action_id"),
                })
                flag(entry, "wrong_official_term")

    duplicate_groups = []
    conflicts = []
    for (language, normalized), group in sorted(normalized_groups.items()):
        if len(group) < 2:
            continue
        record = {
            "language": language,
            "normalized_source": normalized,
            "tm_ids": sorted(str(entry.get("id")) for entry in group),
        }
        duplicate_groups.append(record)
        finals = {str(entry.get("final") or "").strip() for entry in group}
        if len(finals) > 1:
            conflicts.append(record | {"finals": sorted(finals)})
            for entry in group:
                flag(entry, "conflicting_duplicate")
        else:
            for entry in group:
                flag(entry, "normalized_duplicate")

    holdout_leakage = {}
    for language, benchmark in sorted((holdouts or {}).items()):
        confirmed = [
            entry for entry in (benchmark.get("entries") or [])
            if entry.get("gold_status") == "confirmed"
        ]
        leakage = audit_tm_leakage(
            confirmed, memory, language=language,
        )
        holdout_leakage[language] = leakage
        tm_by_id = {
            str(entry.get("id")): entry for entry in entries
        }
        for match in leakage["matches"]:
            tm_entry = tm_by_id.get(match["tm_id"])
            if tm_entry:
                flag(
                    tm_entry, "confirmed_holdout_leakage",
                    holdout_id=match["benchmark_id"],
                )

    for item in candidates.values():
        item["reasons"].sort()
        item["holdout_ids"].sort()
    report = {
        "total_entries": len(entries),
        "approved_by_language": dict(sorted(approved_by_language.items())),
        "unapproved_by_language": dict(sorted(unapproved_by_language.items())),
        "unique_normalized_sources": len(normalized_groups),
        "normalized_duplicate_group_count": len(duplicate_groups),
        "normalized_duplicate_groups": duplicate_groups,
        "conflict_group_count": len(conflicts),
        "conflicts": conflicts,
        "cross_language_pollution_count": len(pollution),
        "cross_language_pollution_ids": pollution,
        "source_damage_count": len(damaged),
        "source_damage_ids": damaged,
        "overlong_count": len(overlong),
        "overlong": overlong,
        "transcript_artifact_count": len(transcript_artifacts),
        "transcript_artifact_ids": transcript_artifacts,
        "missing_required_field_count": len(malformed),
        "missing_required_fields": malformed,
        "stale_official_term_count": len(stale_terms),
        "stale_official_terms": stale_terms,
        "holdout_leakage": holdout_leakage,
        "review_candidate_count": len(candidates),
    }
    return report, sorted(candidates.values(), key=lambda item: item["tm_id"])


def _few_shot_summary(diagnostics_by_language):
    summary = {}
    for language, diagnostics in sorted(diagnostics_by_language.items()):
        batches = diagnostics.get("batches") or []
        selected = [
            item for batch in batches for item in (batch.get("selected") or [])
        ]
        scores = [float(item["score"]) for item in selected]
        reasons = Counter(str(item.get("reason")) for item in selected)
        summary[language] = {
            "approved_memory_count": diagnostics.get("approved_memory_count"),
            "batch_count": len(batches),
            "candidate_total": sum(
                int(batch.get("candidate_count") or 0) for batch in batches
            ),
            "selected_total": len(selected),
            "batches_with_hits": sum(
                bool(batch.get("selected_count")) for batch in batches
            ),
            "score_min": min(scores) if scores else None,
            "score_max": max(scores) if scores else None,
            "reasons": dict(sorted(reasons.items())),
        }
    return summary


def render_markdown(report, few_shot, *, tm_path, evidence_path):
    def display_path(value):
        path = Path(value).resolve()
        try:
            return path.relative_to(BASE).as_posix()
        except ValueError:
            return path.as_posix()

    lines = [
        "# Translation Memory Quality Audit",
        "",
        "This is a read-only mechanical audit. No entry was approved, rejected, "
        "rewritten, or represented as human-reviewed.",
        "",
        f"TM: `{display_path(tm_path)}`",
        "",
        f"Official evidence: `{display_path(evidence_path)}`",
        "",
        "## Inventory",
        "",
        "| language | approved | unapproved |",
        "|---|---:|---:|",
    ]
    languages = sorted(set(report["approved_by_language"]) | set(
        report["unapproved_by_language"]
    ))
    for language in languages:
        lines.append(
            f"| {language} | {report['approved_by_language'].get(language, 0)} "
            f"| {report['unapproved_by_language'].get(language, 0)} |"
        )
    lines.extend([
        "",
        f"Total: {report['total_entries']}; unique normalized sources: "
        f"{report['unique_normalized_sources']}.",
        "",
        "## Deterministic checks",
        "",
        "| check | count |",
        "|---|---:|",
        f"| normalized duplicate groups | {report['normalized_duplicate_group_count']} |",
        f"| conflicting duplicate groups | {report['conflict_group_count']} |",
        f"| cross-language script pollution | {report['cross_language_pollution_count']} |",
        f"| source damage markers | {report['source_damage_count']} |",
        f"| overlong examples | {report['overlong_count']} |",
        f"| transcript artifacts (`>>`) | {report['transcript_artifact_count']} |",
        f"| missing required fields | {report['missing_required_field_count']} |",
        f"| stale official terms | {report['stale_official_term_count']} |",
        "",
        "Transcript markers are review candidates, not proof that a translation "
        "is wrong. Naturalness and correctness remain human judgements.",
        "",
        "## Confirmed-holdout leakage",
        "",
        "| language | checked | approved TM | exact matches |",
        "|---|---:|---:|---:|",
    ])
    for language, leakage in sorted(report["holdout_leakage"].items()):
        lines.append(
            f"| {language} | {leakage['checked_entries']} | "
            f"{leakage['approved_tm_entries']} | {leakage['match_count']} |"
        )
    for language, leakage in sorted(report["holdout_leakage"].items()):
        for match in leakage["matches"]:
            lines.append(
                f"- {language}: benchmark `{match['benchmark_id']}` exactly "
                f"matches TM `{match['tm_id']}` (`{match['normalized_source']}`)."
            )
    lines.extend([
        "",
        "## Few-shot retrieval on fixed Blind3 manifests",
        "",
        "The production diagnostics below use per-cue matching. Weak overlaps "
        "from different cues are no longer aggregated into one match.",
        "",
        "| language | approved | batches | candidates | selected | score range | reason |",
        "|---|---:|---:|---:|---:|---|---|",
    ])
    for language, item in sorted(few_shot.items()):
        score_range = (
            "n/a" if item["score_min"] is None
            else f"{item['score_min']:.4f}–{item['score_max']:.4f}"
        )
        reason = ", ".join(
            f"{name}={count}" for name, count in item["reasons"].items()
        )
        lines.append(
            f"| {language} | {item['approved_memory_count']} | "
            f"{item['batch_count']} | {item['candidate_total']} | "
            f"{item['selected_total']} | {score_range} | {reason} |"
        )
    lines.extend([
        "",
        "Full per-batch sources, scores, and reasons are in "
        "`reports/few_shot_diagnostics_{en,ja,ko}.json`.",
        "",
        "## Disposition",
        "",
        f"`reports/tm_review_candidates.json` contains "
        f"{report['review_candidate_count']} mechanically flagged entries. "
        "No provisional Gold, source-damaged entry, unresolved entity, or "
        "machine preference was written into TM.",
        "",
    ])
    return "\n".join(lines)


def _parse_mapping(values):
    result = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"expected LANGUAGE=PATH: {value}")
        language, path = value.split("=", 1)
        result[language] = Path(path)
    return result


def main():
    parser = argparse.ArgumentParser(description="Read-only TM quality audit")
    parser.add_argument(
        "--tm", type=Path, default=BASE / "data" / "translation_memory.json",
    )
    parser.add_argument(
        "--evidence-file", type=Path,
        default=BASE / "reports" / "automated_official_evidence_20260821.json",
    )
    parser.add_argument("--benchmark", action="append", default=[])
    parser.add_argument("--few-shot", action="append", default=[])
    parser.add_argument(
        "--report", type=Path,
        default=BASE / "reports" / "TM_QUALITY_AUDIT.md",
    )
    parser.add_argument(
        "--review-candidates", type=Path,
        default=BASE / "reports" / "tm_review_candidates.json",
    )
    args = parser.parse_args()
    try:
        benchmark_paths = _parse_mapping(args.benchmark)
        diagnostic_paths = _parse_mapping(args.few_shot)
    except ValueError as error:
        parser.error(str(error))
    memory = json.loads(args.tm.read_text(encoding="utf-8"))
    evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
    holdouts = {
        language: json.loads(path.read_text(encoding="utf-8"))
        for language, path in benchmark_paths.items()
    }
    diagnostics = {
        language: json.loads(path.read_text(encoding="utf-8"))
        for language, path in diagnostic_paths.items()
    }
    report, candidates = audit_translation_memory(
        memory, holdouts=holdouts, evidence=evidence,
    )
    few_shot = _few_shot_summary(diagnostics)
    args.report.write_text(
        render_markdown(
            report, few_shot, tm_path=args.tm,
            evidence_path=args.evidence_file,
        ),
        encoding="utf-8",
    )
    args.review_candidates.write_text(json.dumps({
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": (
            "Mechanical flags only. No approval state was changed and every "
            "candidate still requires genuine human review."
        ),
        "summary": report,
        "few_shot": few_shot,
        "candidates": candidates,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "entries": report["total_entries"],
        "review_candidates": len(candidates),
        "holdout_leaks": {
            language: item["match_count"]
            for language, item in report["holdout_leakage"].items()
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
