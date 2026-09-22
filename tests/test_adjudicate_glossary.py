import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.adjudicate_glossary import (
    GlossaryConflict,
    adjudicate_glossary,
)


def _make_db(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE glossary ("
        "english TEXT NOT NULL, chinese TEXT NOT NULL, "
        "game TEXT NOT NULL DEFAULT 'wuwa', category TEXT DEFAULT '', "
        "source_language TEXT NOT NULL, source_term TEXT NOT NULL, "
        "target_language TEXT NOT NULL, target_term TEXT NOT NULL, "
        "PRIMARY KEY (source_language, source_term, target_language, game))"
    )
    connection.execute(
        "INSERT INTO glossary VALUES (?,?,?,?,?,?,?,?)",
        (
            "Aleph One", "阿列夫一", "wuwa", "boss", "en",
            "Aleph One", "zh-CN", "阿列夫一",
        ),
    )
    connection.commit()
    connection.close()


def _write_evidence(path: Path):
    path.write_text(json.dumps({
        "schema_version": 1,
        "glossary_actions": [
            {
                "action_id": "replace-1",
                "candidate_id": "C031",
                "decision": "VERIFIED_ADD",
                "operation": "replace",
                "source_language": "en",
                "old_source_term": "Aleph One",
                "source_term": "Aleph-1",
                "target_language": "zh-CN",
                "target_term": "阿列夫一",
                "category": "boss",
                "official_url": "https://example.official/item/1",
                "official_title": "Official title",
                "exact_location": "body line 4",
                "accessed_at": "2026-08-21",
                "evidence_summary": "Direct spelling evidence.",
                "confidence": "high",
            },
            {
                "action_id": "add-1",
                "candidate_id": "C021",
                "decision": "VERIFIED_ADD",
                "operation": "add",
                "source_language": "ja",
                "source_term": "ストライダーゲート",
                "target_language": "zh-CN",
                "target_term": "隧门",
                "category": "lore",
                "official_url": "https://example.official/item/2",
                "official_title": "Official title 2",
                "exact_location": "body line 8",
                "accessed_at": "2026-08-21",
                "evidence_summary": "Direct spelling evidence.",
                "confidence": "high",
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _rows(path: Path):
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT source_language, source_term, target_term, category "
        "FROM glossary ORDER BY source_language, source_term"
    ).fetchall()
    connection.close()
    return rows


def test_default_dry_run_writes_report_but_not_database_or_backup(tmp_path):
    database = tmp_path / "glossary.db"
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.md"
    _make_db(database)
    _write_evidence(evidence)
    before = database.read_bytes()

    result = adjudicate_glossary(database, evidence, report)

    assert result["applied"] is False
    assert result["change_count"] == 2
    assert result["backup_path"] is None
    assert database.read_bytes() == before
    assert "DRY RUN" in report.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.bak-*"))


def test_apply_backs_up_then_commits_and_verifies_add_and_replace(tmp_path):
    database = tmp_path / "glossary.db"
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.md"
    _make_db(database)
    _write_evidence(evidence)

    result = adjudicate_glossary(database, evidence, report, apply=True)

    assert result["applied"] is True
    assert result["change_count"] == 2
    assert Path(result["backup_path"]).is_file()
    assert _rows(database) == [
        ("en", "Aleph-1", "阿列夫一", "boss"),
        ("ja", "ストライダーゲート", "隧门", "lore"),
    ]
    assert result["verification"] == {
        "after_total": 2,
        "before_total": 1,
        "expected_total": 2,
        "language_counts": {"en": 1, "ja": 1},
        "verified_action_ids": ["replace-1", "add-1"],
    }
    assert "APPLIED" in report.read_text(encoding="utf-8")


def test_second_apply_is_idempotent_and_does_not_make_redundant_backup(tmp_path):
    database = tmp_path / "glossary.db"
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.md"
    _make_db(database)
    _write_evidence(evidence)
    adjudicate_glossary(database, evidence, report, apply=True)
    first_bytes = database.read_bytes()
    backups_before = list(tmp_path.glob("*.bak-*"))

    second = adjudicate_glossary(database, evidence, report, apply=True)

    assert second["change_count"] == 0
    assert second["already_applied"] == ["replace-1", "add-1"]
    assert second["backup_path"] is None
    assert database.read_bytes() == first_bytes
    assert list(tmp_path.glob("*.bak-*")) == backups_before


def test_conflict_aborts_before_backup_or_any_write(tmp_path):
    database = tmp_path / "glossary.db"
    evidence = tmp_path / "evidence.json"
    report = tmp_path / "report.md"
    _make_db(database)
    _write_evidence(evidence)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO glossary VALUES (?,?,?,?,?,?,?,?)",
        (
            "ストライダーゲート", "错误目标", "wuwa", "lore", "ja",
            "ストライダーゲート", "zh-CN", "错误目标",
        ),
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()

    with pytest.raises(GlossaryConflict, match="add-1"):
        adjudicate_glossary(database, evidence, report, apply=True)

    assert database.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))


def test_help_never_creates_or_opens_database(tmp_path):
    root = Path(__file__).resolve().parents[1]
    missing_database = tmp_path / "must-not-be-created.db"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "adjudicate_glossary.py"),
            "--db", str(missing_database),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert completed.returncode == 0
    assert "--evidence-file" in completed.stdout
    assert not missing_database.exists()


def test_versioned_evidence_records_every_asr_candidate_with_required_fields():
    import pytest
    pytest.skip("Private historical evidence report is not distributed")
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((
        root / "reports" / "automated_official_evidence_20260821.json"
    ).read_text(encoding="utf-8"))
    required = {
        "candidate_id", "language", "source_variant",
        "proposed_canonical_source", "proposed_chinese_target", "decision",
        "official_url", "official_page_or_video_title",
        "exact_location_or_timecode", "accessed_at", "evidence_summary",
        "confidence", "reason_for_not_applying",
    }
    candidates = payload["candidates"]

    assert len(candidates) == 41
    assert {item["candidate_id"] for item in candidates} == {
        f"ASR-{index:03d}" for index in range(1, 42)
    }
    assert all(required <= set(item) for item in candidates)
    assert all(item["decision"] in payload["allowed_decisions"] for item in candidates)
    assert all(item["accessed_at"] and item["evidence_summary"] for item in candidates)
    assert all(
        item["reason_for_not_applying"]
        for item in candidates
        if item["decision"] != "VERIFIED_ADD"
    )
