import csv
import json
from pathlib import Path

import pytest

from tools.export_benchmark_review import REVIEW_FIELDS, export_review
from tools.import_benchmark_review import import_review


def _write_benchmark(path: Path):
    path.write_text(json.dumps({
        "name": "review-fixture",
        "source_language": "ja",
        "target_language": "zh",
        "entries": [
            {
                "id": "pending-1",
                "canonical_source_text": "エイメスです。",
                "gold_zh": "这是爱弥斯。",
                "gold_status": "provisional",
                "entities": ["爱弥斯"],
                "error_tags": [],
                "unrelated": {"preserve": True},
            },
            {
                "id": "confirmed-1",
                "canonical_source_text": "確認済み。",
                "gold_zh": "已确认。",
                "gold_status": "confirmed",
                "entities": [],
                "error_tags": [],
            },
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _update_review(path: Path, **updates):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0].update({key: str(value) for key, value in updates.items()})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_export_review_has_stable_fields_and_filters_status(tmp_path):
    benchmark = tmp_path / "benchmark.json"
    review = tmp_path / "review.csv"
    _write_benchmark(benchmark)

    result = export_review(benchmark, review, gold_status="provisional")

    with review.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == REVIEW_FIELDS
    assert result["row_count"] == 1
    assert rows == [{
        "language": "ja",
        "id": "pending-1",
        "canonical_source_text": "エイメスです。",
        "current_gold_zh": "这是爱弥斯。",
        "gold_status": "provisional",
        "decision": "",
        "corrected_gold_zh": "",
        "review_notes": "",
        "evidence": "",
        "reviewer": "",
        "reviewed_at": "",
    }]


def test_import_defaults_to_dry_run_and_writes_audit_ledger(tmp_path):
    benchmark = tmp_path / "benchmark.json"
    review = tmp_path / "review.csv"
    ledger = tmp_path / "review-ledger.jsonl"
    _write_benchmark(benchmark)
    export_review(benchmark, review, gold_status="provisional")
    _update_review(
        review,
        decision="correct",
        corrected_gold_zh="我是爱弥斯。",
        evidence="official.example/item/1",
        reviewer="Alice Chen",
        reviewed_at="2026-08-21T10:30:00+08:00",
    )
    before = benchmark.read_bytes()

    result = import_review(benchmark, review, ledger)

    assert result["applied"] is False
    assert result["change_count"] == 1
    assert benchmark.read_bytes() == before
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["applied"] is False
    assert records[-1]["changes"][0]["id"] == "pending-1"
    assert records[-1]["changes"][0]["before"]["gold_status"] == "provisional"
    assert records[-1]["changes"][0]["after"] == {
        "gold_zh": "我是爱弥斯。",
        "gold_status": "confirmed",
    }


def test_apply_preserves_unrelated_fields_and_is_idempotent(tmp_path):
    benchmark = tmp_path / "benchmark.json"
    review = tmp_path / "review.csv"
    ledger = tmp_path / "review-ledger.jsonl"
    _write_benchmark(benchmark)
    export_review(benchmark, review, gold_status="provisional")
    _update_review(
        review,
        decision="keep",
        evidence="official.example/item/1",
        reviewer="Alice Chen",
        reviewed_at="2026-08-21T10:30:00+08:00",
    )

    first = import_review(benchmark, review, ledger, apply=True)
    first_bytes = benchmark.read_bytes()
    second = import_review(benchmark, review, ledger, apply=True)

    payload = json.loads(first_bytes)
    pending = payload["entries"][0]
    assert first["change_count"] == 1
    assert pending["gold_zh"] == "这是爱弥斯。"
    assert pending["gold_status"] == "confirmed"
    assert pending["unrelated"] == {"preserve": True}
    assert pending["gold_review"] == {
        "evidence": "official.example/item/1",
        "notes": "",
        "reviewed_at": "2026-08-21T10:30:00+08:00",
        "reviewer": "Alice Chen",
    }
    assert second["change_count"] == 0
    assert benchmark.read_bytes() == first_bytes


@pytest.mark.parametrize("reviewer", ["", "Codex", "ChatGPT", "AI", "human"])
def test_import_rejects_missing_or_automated_reviewer(tmp_path, reviewer):
    benchmark = tmp_path / "benchmark.json"
    review = tmp_path / "review.csv"
    ledger = tmp_path / "review-ledger.jsonl"
    _write_benchmark(benchmark)
    export_review(benchmark, review, gold_status="provisional")
    _update_review(
        review,
        decision="keep",
        evidence="official.example/item/1",
        reviewer=reviewer,
        reviewed_at="2026-08-21T10:30:00+08:00",
    )

    with pytest.raises(ValueError, match="human reviewer"):
        import_review(benchmark, review, ledger, apply=True)


def test_import_rejects_stale_status_language_and_unknown_id(tmp_path):
    benchmark = tmp_path / "benchmark.json"
    review = tmp_path / "review.csv"
    ledger = tmp_path / "review-ledger.jsonl"
    _write_benchmark(benchmark)
    export_review(benchmark, review, gold_status="provisional")
    _update_review(
        review,
        decision="keep",
        evidence="official.example/item/1",
        reviewer="Alice Chen",
        reviewed_at="2026-08-21T10:30:00+08:00",
        gold_status="confirmed",
    )

    with pytest.raises(ValueError, match="stale gold_status"):
        import_review(benchmark, review, ledger, apply=True)

    _update_review(review, gold_status="provisional", language="ko")
    with pytest.raises(ValueError, match="language mismatch"):
        import_review(benchmark, review, ledger, apply=True)

    _update_review(review, language="ja", id="not-present")
    with pytest.raises(ValueError, match="unknown benchmark id"):
        import_review(benchmark, review, ledger, apply=True)
