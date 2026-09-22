"""Auditable human translation memory with an explicit reuse gate."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pipeline.languages import normalize_language_pair


SCHEMA_VERSION = 2
MAX_ENTRIES = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_source(value: str, source_language: str = "en") -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold() if source_language == "en" else text


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TranslationMemoryStore:
    """Store every human correction, but reuse only explicitly approved ones."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def list_all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        version = payload.get("schema_version")
        if version not in {1, SCHEMA_VERSION}:
            raise ValueError("translation memory schema version 不兼容")
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("translation memory entries 格式错误")
        migrated = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            pair = normalize_language_pair(item)
            item.update(pair)
            item["source_normalized"] = item.get("source_normalized") or normalize_source(
                item.get("source", ""), source_language=pair["source_language"],
            )
            migrated.append(item)
        return migrated

    def _save(self, entries: list[dict]) -> None:
        if len(entries) > MAX_ENTRIES:
            approved = [entry for entry in entries if entry.get("approved")]
            unapproved = [entry for entry in entries if not entry.get("approved")]
            keep_unapproved = max(0, MAX_ENTRIES - len(approved))
            entries = approved + unapproved[-keep_unapproved:]
        entries.sort(key=lambda entry: str(entry.get("updated_at", "")))
        _atomic_json(self.path, {
            "schema_version": SCHEMA_VERSION,
            "kind": "translation-memory",
            "entries": entries,
        })

    def record_edit(
        self,
        *,
        source: str,
        previous: str,
        final: str,
        game: str,
        error_type: str,
        job_id: str,
        risk_key: str,
        approve: bool = False,
        source_language: str = "en",
        target_language: str = "zh-CN",
    ) -> dict:
        source = str(source or "").strip()[:4000]
        previous = str(previous or "").strip()[:4000]
        final = str(final or "").strip()[:4000]
        game = str(game or "").strip()[:50] or "default"
        error_type = str(error_type or "其他").strip()[:80] or "其他"
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        source_normalized = normalize_source(
            source, source_language=pair["source_language"],
        )
        if not source_normalized or not final or final == previous:
            raise ValueError("只有包含英文证据且实际改动的译文才能写入翻译记忆")

        identity = hashlib.sha256(
            f"{pair['source_language']}\0{pair['target_language']}\0"
            f"{game.casefold()}\0{source_normalized}\0{final}".encode("utf-8")
        ).hexdigest()[:24]
        entries = self.list_all()
        entry = next(
            (item for item in entries if item.get("id") == identity), None
        )
        timestamp = _now()
        if entry is None:
            entry = {
                "id": identity,
                "source": source,
                "source_normalized": source_normalized,
                "previous": previous,
                "final": final,
                "game": game,
                **pair,
                "error_type": error_type,
                "approved": bool(approve),
                "seen_count": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_job_id": str(job_id or "")[:64],
                "last_risk_key": str(risk_key or "")[:256],
            }
            entries.append(entry)
        else:
            entry["source"] = source
            entry["previous"] = previous
            entry["error_type"] = error_type
            entry.update(pair)
            entry["seen_count"] = int(entry.get("seen_count", 1)) + 1
            entry["updated_at"] = timestamp
            entry["last_job_id"] = str(job_id or "")[:64]
            entry["last_risk_key"] = str(risk_key or "")[:256]
            if approve:
                entry["approved"] = True
                entry["approved_at"] = timestamp
        self._save(entries)
        return dict(entry)

    def approved_for_sources(
        self,
        sources: Iterable[str],
        *,
        game: str | None = None,
        source_language: str = "en",
        target_language: str = "zh-CN",
    ) -> list[dict]:
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        normalized = {
            normalize_source(source, source_language=pair["source_language"])
            for source in sources
            if normalize_source(source, source_language=pair["source_language"])
        }
        game_key = str(game or "").casefold()
        return [
            entry for entry in self.list_all()
            if entry.get("approved")
            and entry.get("source_normalized") in normalized
            and entry.get("source_language") == pair["source_language"]
            and entry.get("target_language") == pair["target_language"]
            and (
                not game_key
                or str(entry.get("game", "")).casefold() == game_key
            )
        ]
