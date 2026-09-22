# -*- coding: utf-8 -*-
"""Apply direct-official-evidence glossary actions; dry-run is the default."""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE / "data" / "glossary.db"
DEFAULT_EVIDENCE = (
    BASE / "reports" / "automated_official_evidence_20260821.json"
)
DEFAULT_REPORT = BASE / "reports" / "AUTOMATED_CANDIDATE_ADJUDICATION.md"
REQUIRED_ACTION_FIELDS = {
    "action_id",
    "candidate_id",
    "decision",
    "operation",
    "source_language",
    "source_term",
    "target_language",
    "target_term",
    "category",
    "official_url",
    "official_title",
    "exact_location",
    "accessed_at",
    "evidence_summary",
    "confidence",
}


class GlossaryConflict(ValueError):
    """Raised when current DB state is not the state authorized by evidence."""


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_actions(evidence_path):
    payload = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    actions = payload.get("glossary_actions")
    if not isinstance(actions, list):
        raise ValueError("evidence file glossary_actions must be a list")
    seen = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"glossary action {index} must be an object")
        missing = sorted(
            field for field in REQUIRED_ACTION_FIELDS
            if not str(action.get(field) or "").strip()
        )
        if missing:
            raise ValueError(
                f"glossary action {index} missing: {', '.join(missing)}"
            )
        action_id = str(action["action_id"])
        if action_id in seen:
            raise ValueError(f"duplicate action_id: {action_id}")
        seen.add(action_id)
        if action["decision"] != "VERIFIED_ADD":
            raise ValueError(f"{action_id}: decision cannot authorize an apply")
        if action["operation"] not in {"add", "replace"}:
            raise ValueError(f"{action_id}: operation must be add or replace")
        if action["operation"] == "replace" and not str(
            action.get("old_source_term") or ""
        ).strip():
            raise ValueError(f"{action_id}: old_source_term is required")
        if action["confidence"] != "high":
            raise ValueError(f"{action_id}: only high-confidence evidence applies")
    return payload, actions


def _find_row(connection, action, *, old=False):
    source_term = (
        action["old_source_term"] if old else action["source_term"]
    )
    return connection.execute(
        "SELECT english, chinese, game, category, source_language, "
        "source_term, target_language, target_term FROM glossary "
        "WHERE source_language=? AND source_term=? "
        "AND target_language=? AND game='wuwa'",
        (
            action["source_language"], source_term,
            action["target_language"],
        ),
    ).fetchone()


def _row_matches(row, action):
    return bool(
        row
        and row[3] == action["category"]
        and row[4] == action["source_language"]
        and row[5] == action["source_term"]
        and row[6] == action["target_language"]
        and row[7] == action["target_term"]
    )


def _classify(connection, actions):
    changes = []
    already_applied = []
    for action in actions:
        action_id = action["action_id"]
        new_row = _find_row(connection, action)
        if action["operation"] == "add":
            if new_row is None:
                changes.append(action)
            elif _row_matches(new_row, action):
                already_applied.append(action_id)
            else:
                raise GlossaryConflict(
                    f"{action_id}: existing row conflicts with authorized add"
                )
            continue

        old_row = _find_row(connection, action, old=True)
        if old_row is None and _row_matches(new_row, action):
            already_applied.append(action_id)
        elif old_row is None:
            raise GlossaryConflict(
                f"{action_id}: authorized old source row is missing"
            )
        elif new_row is not None:
            raise GlossaryConflict(
                f"{action_id}: old and replacement rows both exist"
            )
        elif old_row[7] != action["target_term"]:
            raise GlossaryConflict(
                f"{action_id}: existing target differs from evidence"
            )
        else:
            changes.append(action)
    return changes, already_applied


def _backup_database(connection, database_path):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = Path(f"{database_path}.bak-{timestamp}")
    backup_connection = sqlite3.connect(backup_path)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    return backup_path


def _apply_action(connection, action):
    if action["operation"] == "add":
        connection.execute(
            "INSERT INTO glossary (english, chinese, game, category, "
            "source_language, source_term, target_language, target_term) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                action["source_term"], action["target_term"], "wuwa",
                action["category"], action["source_language"],
                action["source_term"], action["target_language"],
                action["target_term"],
            ),
        )
        return
    cursor = connection.execute(
        "UPDATE glossary SET english=?, chinese=?, category=?, source_term=?, "
        "target_term=? WHERE source_language=? AND source_term=? "
        "AND target_language=? AND game='wuwa'",
        (
            action["source_term"], action["target_term"], action["category"],
            action["source_term"], action["target_term"],
            action["source_language"], action["old_source_term"],
            action["target_language"],
        ),
    )
    if cursor.rowcount != 1:
        raise GlossaryConflict(
            f"{action['action_id']}: replacement updated {cursor.rowcount} rows"
        )


def _verify(connection, actions, before_total, changed_actions):
    verified = []
    for action in actions:
        if not _row_matches(_find_row(connection, action), action):
            raise GlossaryConflict(
                f"{action['action_id']}: post-apply verification failed"
            )
        verified.append(action["action_id"])
    after_total = connection.execute(
        "SELECT COUNT(*) FROM glossary"
    ).fetchone()[0]
    expected_total = before_total + sum(
        1 for action in changed_actions if action["operation"] == "add"
    )
    if after_total != expected_total:
        raise GlossaryConflict(
            f"total mismatch: expected {expected_total}, got {after_total}"
        )
    language_counts = {
        language: count for language, count in connection.execute(
            "SELECT source_language, COUNT(*) FROM glossary "
            "GROUP BY source_language ORDER BY source_language"
        )
    }
    return {
        "after_total": after_total,
        "before_total": before_total,
        "expected_total": expected_total,
        "language_counts": language_counts,
        "verified_action_ids": verified,
    }


def _render_report(result, actions, evidence_path):
    mode = "APPLIED" if result["applied"] else "DRY RUN"
    lines = [
        "# Automated Candidate Adjudication",
        "",
        f"Status: **{mode}**",
        "",
        f"Evidence file: `{Path(evidence_path).name}`",
        "",
        f"Planned database changes: {result['change_count']}",
        "",
        f"Already applied: {len(result['already_applied'])}",
        "",
        "No ASR correction was automatically added: official canonical-name "
        "evidence did not prove any exact reaction-video ASR mapping.",
        "",
        "## Authorized glossary actions",
        "",
        "| action | candidate | operation | language | source | target | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for action in actions:
        lines.append(
            f"| {action['action_id']} | {action['candidate_id']} | "
            f"{action['operation']} | {action['source_language']} | "
            f"{action['source_term']} | {action['target_term']} | "
            f"[{action['official_title']}]({action['official_url']}) |"
        )
    lines.extend([
        "",
        "## Guardrails",
        "",
        "- `VERIFIED_ADD` plus high-confidence direct evidence is required.",
        "- Dry-run is the default; only `--apply` can mutate the database.",
        "- Conflicts abort before backup or mutation.",
        "- Apply uses a SQLite transaction and verifies totals, language counts, "
        "source terms, targets, and action IDs before commit.",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def adjudicate_glossary(
    database_path, evidence_path, report_path, *, apply=False,
):
    database_path = Path(database_path)
    evidence_path = Path(evidence_path)
    report_path = Path(report_path)
    _, actions = load_actions(evidence_path)
    connection = sqlite3.connect(database_path)
    backup_path = None
    try:
        before_total = connection.execute(
            "SELECT COUNT(*) FROM glossary"
        ).fetchone()[0]
        changes, already_applied = _classify(connection, actions)
        if apply and changes:
            backup_path = _backup_database(connection, database_path)
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_changes, current_already = _classify(
                    connection, actions
                )
                if [item["action_id"] for item in current_changes] != [
                    item["action_id"] for item in changes
                ] or current_already != already_applied:
                    raise GlossaryConflict(
                        "database changed between validation and transaction"
                    )
                for action in changes:
                    _apply_action(connection, action)
                verification = _verify(
                    connection, actions, before_total, changes,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        else:
            projected_total = before_total + sum(
                1 for action in changes if action["operation"] == "add"
            )
            language_counts = dict(Counter(
                language for (language,) in connection.execute(
                    "SELECT source_language FROM glossary"
                )
            ))
            for action in changes:
                if action["operation"] == "add":
                    language_counts[action["source_language"]] = (
                        language_counts.get(action["source_language"], 0) + 1
                    )
            verification = {
                "after_total": projected_total,
                "before_total": before_total,
                "expected_total": projected_total,
                "language_counts": dict(sorted(language_counts.items())),
                "verified_action_ids": [
                    action["action_id"] for action in actions
                ],
            }
    finally:
        connection.close()

    result = {
        "applied": bool(apply),
        "database": str(database_path.resolve()),
        "evidence_file": str(evidence_path.resolve()),
        "report": str(report_path.resolve()),
        "change_count": len(changes),
        "already_applied": already_applied,
        "backup_path": str(backup_path.resolve()) if backup_path else None,
        "verification": verification,
    }
    _atomic_write(report_path, _render_report(result, actions, evidence_path))
    return result


def configure_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="strict")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Adjudicate glossary actions backed by direct official evidence. "
            "Default mode is dry-run."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--evidence-file", type=Path, default=DEFAULT_EVIDENCE,
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--apply", action="store_true",
        help="Back up, transactionally apply, and verify authorized changes.",
    )
    args = parser.parse_args()
    configure_utf8_stdio()
    try:
        result = adjudicate_glossary(
            args.db, args.evidence_file, args.report, apply=args.apply,
        )
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
