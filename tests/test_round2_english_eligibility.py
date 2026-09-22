"""回归测试：EN unknown_entity / text_conflict 等高风险必须进入自动 Reviewer。

真实素材实证（2026-08-10 EN lWgwc_xNzrg）：risk.generated.json 65 条
= 35 unknown_entity + 25 text_conflict + 6 person_name_present，但
AUTOMATIC_REVIEW_REASONS 不含 unknown_entity/text_conflict →
strong_queue_items 为空 → Round 2 eligible=0 → English Reviewer 完全
没有执行，所有错译直接交付。Froolova→"那个人" 等专名错误因此漏网。

修复方向：仲裁/实体类风险（unknown_entity、hallucinated_entity、
source_unresolved、text_conflict）必须进入自动 Round 2 审查。
"""
import pytest

from pipeline.long_video import _requires_blocking_round_two


def test_unknown_entity_risk_is_round_two_eligible():
    item = {"reasons": ["unknown_entity:Froolova"], "score": 8}
    assert _requires_blocking_round_two(item), (
        "unknown_entity 风险必须进入自动 Reviewer"
    )


def test_hallucinated_entity_risk_is_round_two_eligible():
    item = {"reasons": ["hallucinated_entity:那个谁"], "score": 10}
    assert _requires_blocking_round_two(item)


def test_text_conflict_risk_is_round_two_eligible():
    item = {"reasons": ["text_conflict"], "score": 3}
    assert _requires_blocking_round_two(item)


def test_source_unresolved_risk_is_round_two_eligible():
    item = {"reasons": ["source_unresolved"], "score": 10}
    assert _requires_blocking_round_two(item)


def test_existing_auto_reasons_still_eligible():
    """原有自动审查原因不得回归。"""
    for reason in ("empty_translation", "number_mismatch", "negation_missing",
                   "incomplete_fragment", "english_residue"):
        assert _requires_blocking_round_two({"reasons": [reason]}), reason


def test_weak_risk_only_remains_deferred():
    """纯人工级风险（person_name_present）仍走人工，不强行进 Round 2。"""
    assert not _requires_blocking_round_two({"reasons": ["person_name_present"]})
