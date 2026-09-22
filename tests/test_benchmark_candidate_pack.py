import csv
import json

from tools.build_benchmark_candidate_pack import build_candidate_pack


def _write_srt(path, rows):
    blocks = []
    for subtitle_id, source in rows:
        blocks.append(
            f"{subtitle_id}\n00:00:{subtitle_id:02d},000 --> "
            f"00:00:{subtitle_id:02d},900\n{source}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def test_candidate_pack_is_unreviewed_and_excludes_benchmark_and_tm_leakage(
        tmp_path):
    job = tmp_path / "job-ja"
    job.mkdir()
    _write_srt(job / "final.zh.ja.union.final.srt", [
        (1, "既存ベンチ"),
        (2, "既存メモリ"),
        (3, "新しい候補です"),
    ])
    _write_srt(job / "final.round2.zh.srt", [
        (1, "现有基准"),
        (2, "现有记忆"),
        (3, "这是新候选"),
    ])
    _write_srt(job / "final.round1.zh.srt", [
        (1, "现有基准"),
        (2, "现有记忆"),
        (3, "这是旧译文"),
    ])
    (job / "holdout_summary.json").write_text(json.dumps({
        "video_id": "video-123", "exit": 0,
    }), encoding="utf-8")
    (job / "final.zh.risk.generated.json").write_text(json.dumps({
        "items": [{
            "key": "risk:3",
            "subtitle_id": 3,
            "reasons": ["literal_translation"],
            "review_required": True,
        }],
    }), encoding="utf-8")
    (job / "final.zh.round2.results.json").write_text(json.dumps({
        "items": [{
            "key": "risk:3",
            "decision": "replace",
            "accepted": True,
            "applied": "round2",
            "round2_status": "replace",
            "state": "resolved",
            "confidence": 0.98,
            "validation_reasons": [],
        }],
    }), encoding="utf-8")
    benchmark = tmp_path / "ja_benchmark.json"
    benchmark.write_text(json.dumps({
        "source_language": "ja",
        "entries": [{"canonical_source_text": "既存ベンチ"}],
    }), encoding="utf-8")
    translation_memory = tmp_path / "translation_memory.json"
    translation_memory.write_text(json.dumps({
        "entries": [{
            "source": "既存メモリ", "source_language": "ja",
            "approved": True,
        }],
    }), encoding="utf-8")
    output_json = tmp_path / "candidates.json"
    output_csv = tmp_path / "review.csv"

    result = build_candidate_pack(
        job, "ja", benchmark, translation_memory,
        output_json=output_json, output_csv=output_csv,
    )

    assert result["candidate_count"] == 1
    assert result["benchmark_overlap_skipped"] == 1
    assert result["tm_overlap_skipped"] == 1
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["id"].startswith("video-123:")
    assert entry["canonical_source_text"] == "新しい候補です"
    assert entry["gold_zh"] == "这是新候选"
    assert entry["round1_zh"] == "这是旧译文"
    assert entry["round2_zh"] == "这是新候选"
    assert entry["machine_changed"] is True
    assert entry["round2_reviews"] == [{
        "key": "risk:3",
        "decision": "replace",
        "accepted": True,
        "applied": "round2",
        "round2_status": "replace",
        "state": "resolved",
        "confidence": 0.98,
        "validation_reasons": [],
    }]
    assert entry["gold_status"] == "provisional"
    assert entry["candidate_status"] == "machine_generated_unreviewed"
    assert entry["human_reviewed"] is False
    assert entry["review_required"] is True
    assert entry["error_tags"] == ["literal_translation"]

    with output_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["current_gold_zh"] == "这是新候选"
    assert rows[0]["decision"] == ""
    assert rows[0]["reviewer"] == ""
    assert rows[0]["reviewed_at"] == ""


def test_candidate_pack_rejects_wrong_language_benchmark(tmp_path):
    job = tmp_path / "job-en"
    job.mkdir()
    _write_srt(job / "final.zh.en.union.final.srt", [(1, "New source")])
    _write_srt(job / "final.round2.zh.srt", [(1, "新来源")])
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({
        "source_language": "ko", "entries": [],
    }), encoding="utf-8")
    tm = tmp_path / "tm.json"
    tm.write_text('{"entries":[]}', encoding="utf-8")

    try:
        build_candidate_pack(job, "en", benchmark, tm)
    except ValueError as error:
        assert "benchmark language" in str(error)
    else:
        raise AssertionError("wrong-language benchmark must be rejected")
