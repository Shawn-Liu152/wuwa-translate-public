"""Fail a release build when a bundled SQLite database contains private data.

Only pattern names and database locations are reported.  Matched values are
never printed, so the audit itself cannot copy a secret into build logs.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path


BASE_PATTERNS = {
    "windows_home": re.compile(
        r"[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+", re.IGNORECASE
    ),
    "unix_home": re.compile(
        r"/(?:Users|home)/[^/\s\"'<>]+", re.IGNORECASE
    ),
    "email": re.compile(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE
    ),
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "google_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return None


def audit_database(path: Path, local_marker: str = "") -> dict[str, object]:
    patterns = dict(BASE_PATTERNS)
    if len(local_marker) >= 3:
        patterns["local_account"] = re.compile(
            r"(?<![A-Z0-9])" + re.escape(local_marker) + r"(?![A-Z0-9])",
            re.IGNORECASE,
        )

    locations: Counter[str] = Counter()
    row_count = 0
    database_uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        schema_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
        for object_name, schema_sql in schema_rows:
            for pattern_name, pattern in patterns.items():
                if pattern.search(schema_sql):
                    locations[f"{pattern_name}:schema.{object_name}"] += 1

        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            quoted_table = _quote_identifier(table)
            cursor = connection.execute(f"SELECT * FROM {quoted_table}")
            columns = [description[0] for description in cursor.description]
            for row in cursor:
                row_count += 1
                for column, value in zip(columns, row):
                    candidate = _text(value)
                    if not candidate:
                        continue
                    for pattern_name, pattern in patterns.items():
                        if pattern.search(candidate):
                            locations[
                                f"{pattern_name}:{table}.{column}"
                            ] += 1

    return {
        "database": path.name,
        "rows_scanned": row_count,
        "hit_count": sum(locations.values()),
        "locations": dict(sorted(locations.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--local-marker", default="")
    args = parser.parse_args()

    result = audit_database(args.database, args.local_marker)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["hit_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
