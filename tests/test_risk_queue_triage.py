"""O-1 triage: derived auto-resolution must shrink the queue, never the gate."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.risk_queue import (
    is_spot_check,
    is_whitelisted_surface,
    load_external_entities,
    triage_risk_item,
)
from pipeline.risk_queue_store import merge_risk_views, save_generated_risks
from pipeline.config import Config
from web import jobs as jobs_module


WHITELIST = {
    "entities": frozenset({"minecraft", "final fantasy", "bandai namco"}),
    "false_positive_words": frozenset({"all", "was", "monster"}),
}

QUEUE_STATES = {"pending", "review", "failed"}


def _item(key, reasons, **overrides):
    payload = {
        "key": key,
        "subtitle_id": 1,
        "start": "00:00:01,000",
        "end": "00:00:02,000",
        "score": 6,
        "reasons": list(reasons),
        "review_required": True,
        "source_language": "en",
        "source_text": "hello",
        "translated": "你好",
        "english": "hello",
        "capcut_en": "hello",
        "whisper_en": "hallo",
        "primary_evidence": "hello",
        "secondary_evidence": "hallo",
    }
    payload.update(overrides)
    return payload


def _merge(items, **kwargs):
    return merge_risk_views(items, (), (), whitelist=WHITELIST, **kwargs)


def _state(items):
    merged = _merge(items)
    return merged[0]["state"], merged[0]


def test_whitelisted_brand_leaves_the_human_queue():
    state, merged = _state([_item("k1", ["unknown_entity:Minecraft"])])
    assert state == "auto_resolved"
    assert merged["auto_resolved_reason"] == "whitelisted_entity"
    assert merged["auto_resolved"] is True


def test_misflagged_common_word_leaves_the_human_queue():
    state, merged = _state([_item("k1", ["unknown_entity:Monster"])])
    assert state == "auto_resolved"
    assert merged["auto_resolved_reason"] == "whitelisted_entity"


def test_multiword_brand_matches_inside_a_longer_surface():
    surface = "From Bandai Namco Entertainment"
    assert is_whitelisted_surface(surface, WHITELIST) is True


def test_cold_or_garbled_name_stays_in_the_human_queue():
    state, merged = _state([_item("k1", ["unknown_entity:Sisney"])])
    assert state in QUEUE_STATES
    assert merged["auto_resolved"] is False


def test_partially_whitelisted_surface_stays_in_the_human_queue():
    state, _ = _state([_item("k1", ["unknown_entity:Minecraft,Sisney"])])
    assert state in QUEUE_STATES


def test_english_source_conflict_leaves_the_human_queue():
    state, merged = _state([_item("k1", ["text_conflict"])])
    assert state == "auto_resolved"
    assert merged["auto_resolved_reason"] == "source_conflict_en"


def test_non_english_source_conflict_stays_in_the_human_queue():
    state, _ = _state([
        _item("k1", ["text_conflict"], source_language="ja"),
    ])
    assert state in QUEUE_STATES


def test_source_conflict_without_capcut_evidence_stays_in_queue():
    state, _ = _state([
        _item("k1", ["text_conflict"], primary_evidence="", capcut_en=""),
    ])
    assert state in QUEUE_STATES


def test_advisory_only_reason_leaves_the_human_queue():
    state, merged = _state([_item("k1", ["reading_speed"])])
    assert state == "auto_resolved"
    assert merged["auto_resolved_reason"] == "advisory_only"


def test_content_level_reason_blocks_auto_resolution():
    for reason in (
        "hallucinated_entity", "person_name_present", "term_mismatch",
        "number_mismatch", "negation_missing", "incomplete_fragment",
        "english_residue",
    ):
        state, merged = _state([_item("k1", [reason, "reading_speed"])])
        assert state in QUEUE_STATES, reason
        assert merged["auto_resolved"] is False


def test_whitelisted_entity_still_blocked_by_content_reason():
    state, _ = _state([
        _item("k1", ["unknown_entity:Minecraft", "hallucinated_entity"]),
    ])
    assert state in QUEUE_STATES


def test_review_required_flag_is_never_cleared():
    merged = _merge([_item("k1", ["unknown_entity:Minecraft"])])
    assert merged[0]["review_required"] is True


def test_human_decision_always_wins_over_triage():
    merged = merge_risk_views(
        [_item("k1", ["unknown_entity:Minecraft"])],
        (),
        [{"key": "k1", "state": "resolved", "text": "人工改过"}],
        whitelist=WHITELIST,
    )
    assert merged[0]["state"] == "resolved"
    assert merged[0]["manual_text"] == "人工改过"
    assert merged[0]["auto_resolved"] is False


def test_spot_check_is_deterministic():
    keys = [f"key-{index}" for index in range(200)]
    first = [is_spot_check(key) for key in keys]
    second = [is_spot_check(key) for key in keys]
    assert first == second


def test_spot_check_rate_is_close_to_five_percent():
    total = 4000
    hits = sum(
        1 for index in range(total) if is_spot_check(f"sample-key-{index}")
    )
    rate = hits / total
    assert 0.03 <= rate <= 0.08


def test_spot_checked_entry_stays_in_the_queue():
    key = next(
        f"probe-{index}" for index in range(500) if is_spot_check(f"probe-{index}")
    )
    merged = _merge([_item(key, ["unknown_entity:Minecraft"])])
    assert merged[0]["spot_check"] is True
    assert merged[0]["state"] in QUEUE_STATES
    assert merged[0]["auto_resolved"] is False


def test_round2_verdict_is_exposed_for_audit():
    merged = merge_risk_views(
        [_item("k1", ["text_conflict"])],
        [{"key": "k1", "decision": "keep", "confidence": 0.95, "state": "review"}],
        (),
        whitelist=WHITELIST,
    )
    assert merged[0]["round2_decision"] == "keep"
    assert merged[0]["round2_confidence"] == 0.95


def test_source_stores_are_left_byte_identical(tmp_path):
    path = tmp_path / "risk.generated.json"
    save_generated_risks(str(path), [_item("k1", ["unknown_entity:Minecraft"])])
    before = path.read_bytes()
    stored = json.loads(path.read_text(encoding="utf-8"))["items"]
    merge_risk_views(stored, (), (), whitelist=WHITELIST)
    assert path.read_bytes() == before


def test_delivery_status_stays_review_required_after_triage():
    metrics = {"quality": {
        "risk_rate": 0.2, "review_required_rate": 0.2, "review_required_count": 300,
    }}
    assert jobs_module._delivery_status(metrics) == "review_required"


def test_shipped_whitelist_loads_and_stays_separate_from_glossary():
    whitelist = load_external_entities()
    assert whitelist["entities"]
    assert whitelist["false_positive_words"]
    assert "wuwa" not in whitelist["entities"]


def test_triage_ignores_entries_that_never_required_review():
    item = _item("k1", ["unknown_entity:Minecraft"], review_required=False)
    assert triage_risk_item(item, WHITELIST)[0] is False


class _ClosingStore:
    def close(self):
        return None


def test_job_manager_risks_exposes_triage_fields(tmp_path, monkeypatch):
    """End-to-end: the shipped whitelist reaches the /api/jobs/<id>/risks view."""
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", tmp_path)
    generated = tmp_path / "final.zh.risk.generated.json"
    save_generated_risks(str(generated), [
        _item("k-cold", ["unknown_entity:Sisney"]),
        _item("k-brand", ["unknown_entity:Minecraft"]),
    ])
    manager = jobs_module.JobManager(_ClosingStore())
    monkeypatch.setattr(manager, "_load", lambda job_id: {
        "id": job_id,
        "artifacts": {"final.zh.risk.generated.json": str(generated)},
    })
    monkeypatch.setattr(
        manager, "_safe_job_path", lambda job_id, path: Path(path),
    )
    rows = {row["key"]: row for row in manager.risks("job-1")}

    assert rows["k-brand"]["state"] == "auto_resolved"
    assert rows["k-brand"]["auto_resolved_reason"] == "whitelisted_entity"
    assert rows["k-cold"]["state"] in QUEUE_STATES
    assert rows["k-cold"]["auto_resolved"] is False
    for row in rows.values():
        assert row["review_required"] is True
        assert "spot_check" in row and "round2_decision" in row


# ---- O-1 激进分流：信任 round2 高置信 keep（scope 由 Config 控制）----


def _r2(key, decision="keep", confidence=0.95, state="review"):
    return {
        "key": key, "decision": decision, "confidence": confidence,
        "state": state,
    }


def _merge_r2(items, round2_items):
    return merge_risk_views(items, round2_items, (), whitelist=WHITELIST)


def test_trusted_keep_all_scope_resolves_blocking_reason(monkeypatch):
    monkeypatch.setattr(Config, "RISK_TRUST_KEEP_SCOPE", "all")
    items = [_item("k1", ["hallucinated_entity:那个谁", "person_name_present"])]
    merged = _merge_r2(items, [_r2("k1", "keep", 0.95)])
    assert merged[0]["state"] == "auto_resolved"
    assert merged[0]["auto_resolved_reason"] == "trusted_keep"
    # 红线：自动放行只改派生 state，review_required 旗标绝不动
    assert merged[0]["review_required"] is True


def test_trusted_keep_requires_high_confidence(monkeypatch):
    monkeypatch.setattr(Config, "RISK_TRUST_KEEP_SCOPE", "all")
    items = [_item("k1", ["hallucinated_entity:那个谁"])]
    merged = _merge_r2(items, [_r2("k1", "keep", 0.85)])  # < 0.9 阈值
    assert merged[0]["state"] in QUEUE_STATES
    assert merged[0]["auto_resolved"] is False


def test_trusted_keep_ignores_replace_decision(monkeypatch):
    monkeypatch.setattr(Config, "RISK_TRUST_KEEP_SCOPE", "all")
    items = [_item("k1", ["term_mismatch"])]
    merged = _merge_r2(items, [_r2("k1", "replace", 0.99)])
    assert merged[0]["state"] in QUEUE_STATES
    assert merged[0]["auto_resolved"] is False


def test_trusted_keep_names_scope_spares_content_reasons(monkeypatch):
    monkeypatch.setattr(Config, "RISK_TRUST_KEEP_SCOPE", "names")
    name_merged = _merge_r2(
        [_item("k1", ["person_name_present"])], [_r2("k1", "keep", 0.95)],
    )
    assert name_merged[0]["state"] == "auto_resolved"
    assert name_merged[0]["auto_resolved_reason"] == "trusted_keep_name"

    content_merged = _merge_r2(
        [_item("k2", ["term_mismatch"])], [_r2("k2", "keep", 0.95)],
    )
    assert content_merged[0]["state"] in QUEUE_STATES
    assert content_merged[0]["auto_resolved"] is False


def test_trusted_keep_off_scope_disables_trust(monkeypatch):
    monkeypatch.setattr(Config, "RISK_TRUST_KEEP_SCOPE", "off")
    items = [_item("k1", ["hallucinated_entity:那个谁"])]
    merged = _merge_r2(items, [_r2("k1", "keep", 0.99)])
    assert merged[0]["state"] in QUEUE_STATES
    assert merged[0]["auto_resolved"] is False
