import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.main import _glossary_usage_metrics
from pipeline.pipeline_manifest import summarize_manifest_metrics
from tools.summarize_job_metrics import resolve_job_paths, summarize_job, write_reports


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_job_summary_keeps_distinct_quality_and_round2_rates(tmp_path):
    job = tmp_path / "job-en"
    job.mkdir()
    _write_json(job / "final.zh.pipeline.metrics.json", {
        "schema_version": 1,
        "round1": {
            "tokens_in": 100,
            "tokens_out": 40,
            "wall_seconds": 3.5,
            "glossary_hit_units": 2,
            "glossary_match_occurrences": 3,
            "translatable_units": 4,
            "average_glossary_terms": 1.5,
            "max_glossary_terms": 2,
        },
        "round2": {"tokens_in": 20, "tokens_out": 8, "wall_seconds": 1.5},
        "quality": {
            "primary_subtitles": 6,
            "union_subtitles": 5,
            "review_required_count": 2,
            "review_required_rate": 0.4,
            "display_errors": 0,
            "display_warnings": 1,
        },
    })
    _write_json(job / "final.round1.zh.srt.manifest.json", {
        "schema_version": 1,
        "options": {"source_language": "en", "model": "unknown-model"},
        "entries": [],
    })
    _write_json(job / "final.zh.risk.generated.json", {
        "schema_version": 1,
        "items": [
            {"reasons": ["source_unresolved"], "review_required": True},
            {"reasons": ["number_mismatch"], "review_required": True},
        ],
    })
    _write_json(job / "final.zh.round2.results.json", {
        "schema_version": 1,
        "metrics": {"round2_summary": {
            "round2_eligible": 2,
            "keep": 1,
            "replace": 1,
            "replace_rejected": 0,
            "unresolved": 0,
            "no_result": 0,
        }},
        "items": [],
    })

    record = summarize_job(job)

    assert record["source_unresolved_count"] == 1
    assert record["source_unresolved_rate"] == 0.2
    assert record["review_required_count"] == 2
    assert record["review_required_rate"] == 0.4
    assert record["round2_no_result_rate"] == 0.0
    assert record["accepted_replace"] == 1
    assert record["accepted_replace_rate"] == 0.5
    assert record["effective_fix_rate"] is None
    assert record["effective_fix_rate_observation"] == "not_observable"
    assert record["estimated_cost"] is None
    assert record["cost_availability"] == "unavailable"


def test_job_summary_does_not_erase_historical_wall_time_after_resume(tmp_path):
    job = tmp_path / "resumed-ja"
    job.mkdir()
    _write_json(job / "final.zh.pipeline.metrics.json", {
        "round1": {"tokens_in": 100, "tokens_out": 40, "wall_seconds": 0.005},
        "round2": {"tokens_in": 20, "tokens_out": 8, "wall_seconds": 0.0},
    })
    _write_json(job / "final.round1.zh.srt.manifest.json", {
        "options": {"source_language": "ja", "model": "unknown-model"},
        "metrics": {"wall_seconds": 0.005},
        "batches": {
            "0": {"state": "success", "metrics": {"elapsed_seconds": 39.854}},
            "1": {"state": "success", "metrics": {"elapsed_seconds": 6.715}},
        },
        "entries": [],
    })
    _write_json(job / "final.round2.zh.srt.manifest.json", {
        "options": {"source_language": "ja", "model": "unknown-model"},
        "metrics": {"wall_seconds": 0.0},
        "batches": {
            "0": {"state": "success", "metrics": {"elapsed_seconds": 20.514}},
        },
        "entries": [],
    })

    record = summarize_job(job)

    assert record["round1_wall_seconds"] == 39.854
    assert record["round2_wall_seconds"] == 20.514
    assert record["total_wall_seconds"] == 60.368


def test_job_reports_write_jsonl_csv_and_markdown(tmp_path):
    records = [{
        "job_id": "job-a",
        "source_language": "en",
        "accepted_replace": 1,
        "effective_fix_rate": None,
        "effective_fix_rate_observation": "not_observable",
    }]

    paths = write_reports(records, tmp_path)

    assert set(paths) == {"jsonl", "csv", "markdown"}
    assert json.loads(paths["jsonl"].read_text(encoding="utf-8")) == records[0]
    assert "job_id,source_language" in paths["csv"].read_text(encoding="utf-8-sig")
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "# Job Metrics Summary" in markdown
    assert "accepted_replace is a deterministic gate outcome" in markdown
    assert "not_observable" in markdown


def test_future_manifest_records_unit_level_glossary_usage():
    class _Sub:
        def __init__(self, text):
            self.text = text

    usage = _glossary_usage_metrics(
        [_Sub("Rover meets Rover."), _Sub("Discover the route.")],
        {"Rover": "漂泊者"},
        "en",
    )
    assert usage == {
        "translatable_units": 2,
        "glossary_hit_units": 1,
        "glossary_match_occurrences": 2,
    }

    manifest = {"batches": {
        "0": {"state": "success", "metrics": {**usage, "glossary_terms": 1}},
        "1": {"state": "success", "metrics": {
            "translatable_units": 1,
            "glossary_hit_units": 1,
            "glossary_match_occurrences": 1,
            "glossary_terms": 2,
        }},
    }}
    summary = summarize_manifest_metrics(manifest)
    assert summary["translatable_units"] == 3
    assert summary["glossary_hit_units"] == 2
    assert summary["glossary_match_occurrences"] == 3


def test_job_path_resolution_accepts_ids_and_scans_download_root(tmp_path):
    download = tmp_path / "download"
    download.mkdir()
    first = download / "job-a"
    first.mkdir()
    _write_json(first / "final.zh.pipeline.metrics.json", {})
    second = download / "job-b"
    second.mkdir()
    _write_json(second / "final.round1.zh.srt.manifest.json", {})
    ignored = download / "not-a-job"
    ignored.mkdir()

    assert resolve_job_paths(["job-b"], download) == [second.resolve()]
    assert resolve_job_paths([], download) == [first.resolve(), second.resolve()]


def test_legacy_job_glossary_metrics_are_read_only_backfill_with_hash(tmp_path):
    job = tmp_path / "legacy-en"
    job.mkdir()
    _write_json(job / "final.round1.zh.srt.manifest.json", {
        "options": {"source_language": "en", "model": "model-a"},
        "entries": [
            {"original": "Rover meets Rover."},
            {"original": "No named terms here."},
        ],
        "metrics": {},
    })
    glossary = tmp_path / "glossary.db"
    connection = sqlite3.connect(glossary)
    connection.execute(
        "CREATE TABLE glossary (source_term TEXT, source_language TEXT, game TEXT)"
    )
    connection.execute(
        "INSERT INTO glossary VALUES (?, ?, ?)", ("Rover", "en", "wuwa")
    )
    connection.commit()
    connection.close()

    record = summarize_job(job, glossary_db=glossary)

    assert record["glossary_hit_units"] == 1
    assert record["glossary_match_occurrences"] == 2
    assert record["glossary_hit_rate"] == 0.5
    assert record["glossary_metrics_basis"] == "current_glossary_read_only_backfill"
    assert len(record["glossary_reference_sha256"]) == 64


def test_job_summary_uses_final_srt_audit_for_residue_and_critical_errors(tmp_path):
    job = tmp_path / "job-ja"
    job.mkdir()
    _write_json(job / "final.zh.pipeline.metrics.json", {
        "quality": {"display_errors": 0, "display_warnings": 7},
    })
    _write_json(job / "blind_audit.json", {
        "source_residual": 2,
        "overlap": 1,
        "nonpositive_duration": 1,
        "split_version_numbers": [["3", "1"]],
        "exact_duplicate_groups": 4,
        "trailing_full_stop": 9,
    })

    record = summarize_job(job)

    assert record["source_residue"] == 2
    assert record["critical_error_count"] == 5
    assert record["display_warnings"] == 7


@pytest.mark.parametrize(("job_id", "language", "source", "union", "review", "eligible", "keep", "replace", "rejected", "no_result"), [
    ("blind3-wJQJaAMLHGg", "en", 304, 202, 80, 70, 65, 5, 7, 0),
    ("blind3-i8jT8mEQMWY", "ja", 205, 106, 56, 30, 26, 0, 1, 10),
    ("blind3-7ThUaoGwU40-ko-boundary-fixed", "ko", 252, 169, 24, 19, 4, 0, 5, 13),
])
def test_blind3_metrics_snapshot_regression(
    tmp_path, job_id, language, source, union, review, eligible, keep,
    replace, rejected, no_result,
):
    root = Path(__file__).resolve().parents[1]
    job = tmp_path / job_id
    job.mkdir()
    _write_json(job / "final.zh.pipeline.metrics.json", {
        "quality": {
            "primary_subtitles": source,
            "union_subtitles": union,
            "review_required_count": review,
            "display_errors": 0,
        },
    })
    _write_json(job / "final.round1.zh.srt.manifest.json", {
        "options": {"source_language": language, "model": "snapshot-model"},
        "entries": [{"original": ""}],
    })
    _write_json(job / "final.zh.round2.results.json", {
        "metrics": {"round2_summary": {
            "round2_eligible": eligible,
            "keep": keep,
            "replace": replace,
            "replace_rejected": rejected,
            "unresolved": 0,
            "no_result": no_result,
            "deferred_weak_risk": 0,
        }},
    })
    _write_json(job / "blind_audit.json", {
        "source_residual": 0,
        "overlap": 0,
        "nonpositive_duration": 0,
        "split_version_numbers": [],
    })

    record = summarize_job(job, glossary_db=root / "data" / "glossary.db")

    assert record["source_language"] == language
    assert record["source_units"] == source
    assert record["union_units"] == union
    assert record["review_required_count"] == review
    assert record["round2_eligible"] == eligible
    assert record["keep"] == keep
    assert record["accepted_replace"] == replace
    assert record["replace_rejected"] == rejected
    assert record["no_result"] == no_result
    assert record["source_residue"] == 0
    assert record["critical_error_count"] == 0
    assert record["glossary_metrics_basis"] == "current_glossary_read_only_backfill"


def test_empty_and_missing_artifacts_are_reported_without_invented_values(tmp_path):
    job = tmp_path / "empty-job"
    job.mkdir()
    (job / "final.zh.pipeline.metrics.json").write_text("", encoding="utf-8")

    record = summarize_job(job, glossary_db=tmp_path / "missing.db")

    assert record["source_language"] == "unknown"
    assert record["source_units"] == 0
    assert record["source_unresolved_rate"] is None
    assert record["glossary_hit_rate"] is None
    assert record["glossary_metrics_basis"] == "unavailable"
    assert record["estimated_cost"] is None
    assert record["cost_availability"] == "unavailable"
    assert record["source_residue"] is None


def test_old_round2_schema_is_inferred_without_merging_no_result(tmp_path):
    job = tmp_path / "old-schema"
    job.mkdir()
    _write_json(job / "final.zh.round2.results.json", {
        "items": [
            {"decision": "keep"},
            {"decision": "replace", "accepted": True},
            {"decision": "replace", "accepted": False},
            {"state": "on_demand"},
            {},
        ],
    })

    record = summarize_job(job, glossary_db=tmp_path / "missing.db")

    assert record["round2_eligible"] == 4
    assert record["keep"] == 1
    assert record["accepted_replace"] == 1
    assert record["replace_rejected"] == 1
    assert record["no_result"] == 1
    assert record["round2_no_result_rate"] == 0.25


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_metrics_cli_direct_execution_can_run_final_srt_audit(tmp_path):
    root = Path(__file__).resolve().parents[1]
    download = tmp_path / "download"
    job = download / "job-ko"
    job.mkdir(parents=True)
    _write_json(job / "final.round1.zh.srt.manifest.json", {
        "options": {"source_language": "ko", "model": "model-a"},
        "entries": [{"original": "안녕"}],
    })
    (job / "final.zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n",
        encoding="utf-8",
    )
    output = tmp_path / "输出报告"

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "summarize_job_metrics.py"),
            "--download-root", str(download),
            "--output-dir", str(output),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((output / "job_metrics.jsonl").read_text(encoding="utf-8"))
    assert payload["source_residue"] == 0
