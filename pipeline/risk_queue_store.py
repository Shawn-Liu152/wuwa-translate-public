"""Persistence for generated risks, automatic review results and human state."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pipeline.risk_queue import (
    RiskItem,
    is_spot_check,
    load_external_entities,
    triage_risk_item,
)

LEGACY_SCHEMA_VERSION = 1
GENERATED_SCHEMA_VERSION = 2
ROUND2_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1

_AUTOMATIC_FIELDS = {
    "state", "round2_text", "local_evidence", "local_evidence_text",
    "local_error", "note",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_payload(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


# ---- Legacy combined queue -------------------------------------------------

def save_risk_queue(path: str, items: Iterable[RiskItem]) -> None:
    """Write the pre-v2 combined queue for backward compatibility."""
    _atomic_payload(path, {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "items": [
            item.to_dict() if isinstance(item, RiskItem) else dict(item)
            for item in items
        ],
    })


def load_risk_queue(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise ValueError("risk queue schema version 不兼容")
    return payload.get("items", [])


def update_risk_item(path: str, key: str, **updates) -> list[dict]:
    items = load_risk_queue(path)
    for item in items:
        if item.get("key") == key:
            item.update(updates)
            save_risk_queue(path, items)
            return items
    raise KeyError(f"风险项不存在: {key}")


# ---- Split v2 stores -------------------------------------------------------

def save_generated_risks(path: str, items: Iterable[RiskItem | dict]) -> None:
    """Write the deterministic risk snapshot without mutable review fields."""
    generated = []
    for item in items:
        raw = item.to_dict() if isinstance(item, RiskItem) else dict(item)
        generated.append({
            key: value for key, value in raw.items()
            if key not in _AUTOMATIC_FIELDS
        })
    _atomic_payload(path, {
        "schema_version": GENERATED_SCHEMA_VERSION,
        "kind": "risk.generated",
        "items": generated,
    })


def load_generated_risks(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("schema_version") != GENERATED_SCHEMA_VERSION:
        raise ValueError("generated risk schema version 不兼容")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("generated risk items 格式错误")
    return items


def save_round2_results(
    path: str,
    items: Iterable[dict],
    *,
    metrics: dict | None = None,
    risk_sha256: str = "",
) -> None:
    _atomic_payload(path, {
        "schema_version": ROUND2_SCHEMA_VERSION,
        "kind": "round2.results",
        "risk_sha256": risk_sha256,
        "metrics": metrics or {},
        "items": list(items),
    })


def load_round2_results(path: str) -> dict:
    target = Path(path)
    if not target.exists():
        return {"items": [], "metrics": {}, "risk_sha256": ""}
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema_version") != ROUND2_SCHEMA_VERSION:
        raise ValueError("round2 results schema version 不兼容")
    if not isinstance(payload.get("items", []), list):
        raise ValueError("round2 results items 格式错误")
    return payload


def ensure_review_state(path: str) -> None:
    """Create the human-owned state file once; never reset it automatically."""
    if Path(path).exists():
        return
    _atomic_payload(path, {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "kind": "review-state",
        "items": [],
    })


def load_review_state(path: str) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError("review state schema version 不兼容")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("review state items 格式错误")
    return items


def update_review_state(path: str, key: str, **changes) -> list[dict]:
    """Update only the human-owned decision for one generated risk key."""
    items = load_review_state(path)
    item = next((entry for entry in items if entry.get("key") == key), None)
    if item is None:
        item = {"key": key}
        items.append(item)
    item.update(changes)
    item["updated_at"] = _utc_now()
    _atomic_payload(path, {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "kind": "review-state",
        "items": items,
    })
    return items


def update_review_states(path: str, updates: Iterable[dict]) -> list[dict]:
    """Apply multiple human decisions with one atomic file replacement."""
    items = load_review_state(path)
    by_key = {
        str(item.get("key")): item for item in items if item.get("key")
    }
    updated_at = _utc_now()
    for change in updates:
        key = str(change.get("key", "")).strip()
        if not key:
            continue
        item = by_key.setdefault(key, {"key": key})
        for field in ("state", "text", "note"):
            if field in change:
                item[field] = change[field]
        item["updated_at"] = updated_at
    merged = list(by_key.values())
    _atomic_payload(path, {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "kind": "review-state",
        "items": merged,
    })
    return merged


def merge_risk_views(
    generated: Iterable[dict],
    round2_items: Iterable[dict] = (),
    review_items: Iterable[dict] = (),
    whitelist: dict | None = None,
) -> list[dict]:
    """Build the mutable Web/API view without changing any source store.

    ``whitelist`` overrides the ``data/external_entities.json`` lookup and is
    only meant for tests; callers leave it at ``None``.
    """
    automatic_by_key = {
        str(item.get("key")): item for item in round2_items
        if item.get("key")
    }
    review_by_key = {
        str(item.get("key")): item for item in review_items
        if item.get("key")
    }
    merged = []
    if whitelist is None:
        whitelist = load_external_entities()
    for generated_item in generated:
        item = dict(generated_item)
        key = str(item.get("key", ""))
        automatic = automatic_by_key.get(key, {})
        review = review_by_key.get(key, {})

        for field in (
            "local_evidence", "local_evidence_text", "local_error",
            "round2_text",
        ):
            if field in automatic:
                item[field] = automatic[field]
        item["automatic_state"] = automatic.get(
            "state", item.get("state", "pending")
        )
        # O-1 audit fields: derived from the round2 verdict, written to the
        # view only.  The three source stores are never rewritten.
        item["round2_decision"] = automatic.get("decision", "")
        item["round2_confidence"] = automatic.get("confidence")
        item["auto_resolved"] = False
        item["auto_resolved_reason"] = ""
        item["spot_check"] = False
        item["manual_text"] = str(review.get("text", "")).strip()
        item["note"] = str(review.get("note", "")).strip()
        # Human decisions remain authoritative. Without one, derive a display
        # state that distinguishes actual work from auto-resolved/advisory
        # entries instead of presenting every generated signal as pending.
        if review.get("state"):
            item["state"] = review["state"]
        else:
            derived = "pending"
            if item["automatic_state"] == "resolved":
                derived = "auto_resolved"
            elif item["automatic_state"] in {"review", "failed"}:
                derived = "review"
            elif not bool(item.get("review_required", False)):
                derived = "advisory"
            item["state"] = derived
            # O-1 triage: entries whose every review-driving signal is already
            # explained (known external brand, resolved English source
            # conflict, or advisory-only hint) leave the human queue.  The
            # ``review_required`` flag is deliberately left untouched, so
            # ``_delivery_status`` keeps returning ``review_required``.
            if derived in {"pending", "review", "failed"} and item.get(
                "review_required", False
            ):
                resolvable, why = triage_risk_item(item, whitelist)
                if resolvable:
                    if is_spot_check(key):
                        item["spot_check"] = True
                    else:
                        item["state"] = "auto_resolved"
                        item["auto_resolved"] = True
                        item["auto_resolved_reason"] = why
        merged.append(item)
    return merged
