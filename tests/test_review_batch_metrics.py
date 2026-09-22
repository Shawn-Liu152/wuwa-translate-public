import csv
import json

import pytest

from tools.prepare_review_batch import REVIEW_BATCH_FIELDS
from tools.summarize_review_batch import summarize_review_batch


def _write_review_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_BATCH_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(entry_id, *, changed=True, decision="", reviewer=""):
    return {
        "language": "en",
        "id": entry_id,
        "review_stratum": "semantic",
        "canonical_source_text": "I am not ready",
        "round1_zh": "我准备好了",
        "round2_zh": "我还没准备好",
        "machine_changed": str(changed).lower(),
        "error_tags": '["negation_missing"]',
        "round2_reviews": "[]",
        "review_decision": decision,
        "final_zh": "",
        "source_quality": "",
        "review_notes": "",
        "evidence": "source subtitle and video context" if decision else "",
        "reviewer": reviewer,
        "reviewed_at": "2026-08-24T10:00:00+08:00" if decision else "",
    }


def test_blank_review_batch_keeps_correctness_not_observable(tmp_path):
    review_csv = tmp_path / "review.csv"
    output_json = tmp_path / "metrics.json"
    output_report = tmp_path / "metrics.md"
    _write_review_csv(review_csv, [_row("one"), _row("two")])

    result = summarize_review_batch(
        review_csv, output_json=output_json, output_report=output_report,
    )

    assert result["selected_count"] == 2
    assert result["reviewed_count"] == 0
    assert result["effective_fix_rate"] is None
    assert result["effective_fix_rate_observation"] == "not_observable"
    persisted = json.loads(output_json.read_text(encoding="utf-8"))
    assert persisted == result
    report = output_report.read_text(encoding="utf-8")
    assert "No genuine human labels are present" in report


def test_review_metrics_separate_effective_harmful_and_source_error_labels(
        tmp_path):
    review_csv = tmp_path / "review.csv"
    output_json = tmp_path / "metrics.json"
    output_report = tmp_path / "metrics.md"
    rows = [
        _row("fixed", decision="round2_preferred", reviewer="Alice Chen"),
        _row("harmed", decision="round1_preferred", reviewer="Alice Chen"),
        _row("missed", decision="both_wrong", reviewer="Alice Chen"),
        _row("bad-source", decision="source_error", reviewer="Alice Chen"),
        _row(
            "both-good", changed=False, decision="both_correct",
            reviewer="Alice Chen",
        ),
        _row("pending"),
    ]
    rows[2]["final_zh"] = "我还没有准备好"
    rows[3]["source_quality"] = "unusable"
    rows[4]["round2_zh"] = rows[4]["round1_zh"]
    _write_review_csv(review_csv, rows)

    result = summarize_review_batch(
        review_csv, output_json=output_json, output_report=output_report,
    )

    assert result["selected_count"] == 6
    assert result["reviewed_count"] == 5
    assert result["source_error_count"] == 1
    assert result["changed_reviewed_count"] == 3
    assert result["effective_fix_count"] == 1
    assert result["harmful_change_count"] == 1
    assert result["unfixed_count"] == 1
    assert result["effective_fix_rate"] == 0.333333
    assert result["harmful_change_rate"] == 0.333333
    assert result["effective_fix_rate_observation"] == "human_observed"
    assert result["round1_accuracy"] == 0.5
    assert result["round2_accuracy"] == 0.5
    report = output_report.read_text(encoding="utf-8")
    assert "Validated genuine-human labels: 5" in report
    assert "Round 2 accuracy: 0.5" in report


def test_review_metrics_reject_preference_when_machine_texts_are_identical(
        tmp_path):
    review_csv = tmp_path / "review.csv"
    row = _row(
        "unchanged-preference",
        changed=False,
        decision="round2_preferred",
        reviewer="Alice Chen",
    )
    row["round2_zh"] = row["round1_zh"]
    _write_review_csv(review_csv, [row])

    with pytest.raises(ValueError, match="identical machine texts"):
        summarize_review_batch(
            review_csv,
            output_json=tmp_path / "metrics.json",
            output_report=tmp_path / "metrics.md",
        )


def test_review_metrics_reject_incorrect_machine_changed_flag(tmp_path):
    review_csv = tmp_path / "review.csv"
    row = _row(
        "bad-change-flag",
        changed=False,
        decision="both_correct",
        reviewer="Alice Chen",
    )
    _write_review_csv(review_csv, [row])

    with pytest.raises(ValueError, match="machine_changed disagrees"):
        summarize_review_batch(
            review_csv,
            output_json=tmp_path / "metrics.json",
            output_report=tmp_path / "metrics.md",
        )


@pytest.mark.parametrize("reviewer", ["AI", "ChatGPT", "Codex", "human"])
def test_review_metrics_reject_automated_or_placeholder_reviewer(
        tmp_path, reviewer):
    review_csv = tmp_path / "review.csv"
    _write_review_csv(
        review_csv,
        [_row("fake-reviewer", decision="both_correct", reviewer=reviewer)],
    )

    with pytest.raises(ValueError, match="genuine human reviewer"):
        summarize_review_batch(
            review_csv,
            output_json=tmp_path / "metrics.json",
            output_report=tmp_path / "metrics.md",
        )
