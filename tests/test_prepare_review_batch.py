import csv
import json

import pytest

from tools.prepare_review_batch import prepare_review_batch


def _write_json(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _entry(entry_id, source, tags):
    return {
        "id": entry_id,
        "canonical_source_text": source,
        "gold_zh": f"译文-{entry_id}",
        "round1_zh": f"初译-{entry_id}",
        "round2_zh": f"译文-{entry_id}",
        "machine_changed": True,
        "round2_reviews": [],
        "gold_status": "provisional",
        "candidate_status": "machine_generated_unreviewed",
        "human_reviewed": False,
        "review_required": True,
        "error_tags": tags,
        "provenance": {"job_id": "job-en", "video_id": "video-en"},
    }


def test_prepare_review_batch_is_stratified_leakage_checked_and_blank(tmp_path):
    candidates = tmp_path / "candidates-en.json"
    benchmark = tmp_path / "benchmark-en.json"
    memory = tmp_path / "translation-memory.json"
    output_json = tmp_path / "review-batch.json"
    output_csv = tmp_path / "review-batch.csv"
    output_report = tmp_path / "review-batch.md"

    _write_json(candidates, {
        "source_language": "en",
        "target_language": "zh-CN",
        "human_reviewed": False,
        "entries": [
            _entry("benchmark-leak", "Known benchmark", []),
            _entry("tm-leak", "Known memory", []),
            _entry("entity", "Xyzzqz arrived", ["unknown_entity:Xyzzqz"]),
            _entry("semantic", "I am not ready", ["negation_missing"]),
            _entry("clean", "This looks amazing", []),
            _entry("extra-clean", "What a trailer", []),
        ],
    })
    _write_json(benchmark, {
        "source_language": "en",
        "entries": [{"canonical_source_text": "Known benchmark"}],
    })
    _write_json(memory, {"entries": [{
        "source": "Known memory",
        "source_language": "en",
        "approved": True,
    }]})

    result = prepare_review_batch(
        {"en": candidates},
        {"en": benchmark},
        memory,
        per_language=3,
        output_json=output_json,
        output_csv=output_csv,
        output_report=output_report,
    )

    assert result["selected_by_language"] == {"en": 3}
    assert result["leakage_skipped"] == {
        "benchmark": 1,
        "approved_tm": 1,
        "duplicate_source": 0,
    }
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["human_reviewed"] is False
    assert payload["review_status"] == "awaiting_genuine_human_review"
    assert {entry["review_stratum"] for entry in payload["entries"]} == {
        "clean_control", "entity", "semantic",
    }
    assert all(entry["human_reviewed"] is False for entry in payload["entries"])

    with output_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert all(row["review_decision"] == "" for row in rows)
    assert all(row["final_zh"] == "" for row in rows)
    assert all(row["reviewer"] == "" for row in rows)
    assert all(row["reviewed_at"] == "" for row in rows)
    report = output_report.read_text(encoding="utf-8")
    assert "No human correctness metric is observable yet" in report
    assert "entity" in report


def test_prepare_review_batch_rejects_reviewed_candidate_contamination(tmp_path):
    candidates = tmp_path / "candidates-en.json"
    benchmark = tmp_path / "benchmark-en.json"
    memory = tmp_path / "translation-memory.json"
    contaminated = _entry("confirmed", "Already reviewed", [])
    contaminated["human_reviewed"] = True
    contaminated["gold_status"] = "confirmed"
    _write_json(candidates, {
        "source_language": "en",
        "human_reviewed": False,
        "entries": [contaminated],
    })
    _write_json(benchmark, {"source_language": "en", "entries": []})
    _write_json(memory, {"entries": []})

    with pytest.raises(ValueError, match="contamination"):
        prepare_review_batch(
            {"en": candidates}, {"en": benchmark}, memory,
            per_language=1,
            output_json=tmp_path / "batch.json",
            output_csv=tmp_path / "batch.csv",
            output_report=tmp_path / "batch.md",
        )


def test_prepare_review_batch_rejects_duplicate_candidate_ids(tmp_path):
    candidates = tmp_path / "candidates-en.json"
    benchmark = tmp_path / "benchmark-en.json"
    memory = tmp_path / "translation-memory.json"
    _write_json(candidates, {
        "source_language": "en",
        "human_reviewed": False,
        "entries": [
            _entry("duplicate", "First source", []),
            _entry("duplicate", "Second source", ["negation_missing"]),
        ],
    })
    _write_json(benchmark, {"source_language": "en", "entries": []})
    _write_json(memory, {"entries": []})

    with pytest.raises(ValueError, match="duplicate candidate id"):
        prepare_review_batch(
            {"en": candidates}, {"en": benchmark}, memory,
            per_language=2,
            output_json=tmp_path / "batch.json",
            output_csv=tmp_path / "batch.csv",
            output_report=tmp_path / "batch.md",
        )
