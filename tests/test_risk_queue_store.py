import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.risk_queue import RiskItem
from pipeline.risk_queue_store import (
    ensure_review_state,
    load_generated_risks,
    load_review_state,
    merge_risk_views,
    save_generated_risks,
    save_risk_queue,
    save_round2_results,
    update_review_state,
    load_risk_queue,
)


def test_risk_queue_json_roundtrip():
    item = RiskItem(
        key="risk-1", subtitle_id=1, start="00:00:00,000", end="00:00:01,000",
        score=5, reasons=["number_mismatch"], english="C6", capcut_en="C6",
        whisper_en="C6", translated="C5",
    )
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as file:
        path = file.name
    try:
        save_risk_queue(path, [item])
        loaded = load_risk_queue(path)
        assert loaded[0]["key"] == "risk-1"
        assert loaded[0]["state"] == "pending"
    finally:
            os.unlink(path)


def test_generated_automatic_and_human_state_are_independent(tmp_path):
    generated_path = tmp_path / "risk.generated.json"
    results_path = tmp_path / "round2.results.json"
    review_path = tmp_path / "review-state.json"
    item = RiskItem(
        key="risk-1", subtitle_id=1,
        start="00:00:00,000", end="00:00:01,000",
        score=5, reasons=["term_mismatch"], english="Rover",
        capcut_en="Rover", whisper_en="Rover", translated="流浪者",
    )
    save_generated_risks(str(generated_path), [item])
    generated_before = generated_path.read_bytes()
    save_round2_results(str(results_path), [{
        "key": "risk-1", "state": "resolved", "round2_text": "漂泊者",
    }])
    ensure_review_state(str(review_path))
    update_review_state(
        str(review_path), "risk-1",
        state="resolved", text="人工：漂泊者",
    )

    assert generated_path.read_bytes() == generated_before
    generated = load_generated_risks(str(generated_path))
    review = load_review_state(str(review_path))
    merged = merge_risk_views(
        generated,
        [{"key": "risk-1", "state": "resolved", "round2_text": "漂泊者"}],
        review,
    )
    assert "state" not in generated[0]
    assert merged[0]["automatic_state"] == "resolved"
    assert merged[0]["state"] == "resolved"
    assert merged[0]["manual_text"] == "人工：漂泊者"


def test_derived_states_hide_auto_resolved_and_advisory_from_work_queue():
    generated = [
        {
            "key": "strong",
            "review_required": True,
            "reasons": ["term_mismatch"],
        },
        {
            "key": "weak",
            "review_required": False,
            "reasons": ["text_conflict"],
        },
    ]

    merged = merge_risk_views(
        generated,
        [
            {"key": "strong", "state": "resolved", "round2_text": "长离"},
            {"key": "weak", "state": "on_demand"},
        ],
    )

    assert merged[0]["state"] == "auto_resolved"
    assert merged[1]["state"] == "advisory"


if __name__ == "__main__":
    test_risk_queue_json_roundtrip()
    print("[PASS] risk queue store tests")
