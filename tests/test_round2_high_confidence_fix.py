"""回归测试：Reviewer gate 不得拒绝高置信的正确修复。

真实素材实证（2026-08-10 JA gl8vjOL1xWI）：6 个 harmful rejections 中
4 个是 confidence 0.97-0.98 的正确修复被 excessive_rewrite（相似度 <0.3）
硬拒——当 Round 1 本身错得离谱时，正确修复与错误原文差异大是必然。

修复：confidence >= 0.95 的高置信修正跳过 excessive_rewrite 相似度检查
（与 semantic marker 的 high-confidence 例外一致）。
"""
import pytest

from pipeline.long_video import _evaluate_round_two_decision


def _ja_item(**overrides):
    item = {
        "subtitle_id": 77,
        "source_language": "ja",
        "source_text": "完璧ではないっていうのを強く思い知ったわけでしょう。結構苦しいことを聞きます",
        "english": "完璧ではないっていうのを強く思い知ったわけでしょう。結構苦しいことを聞きます",
        "primary_evidence": "完璧ではないっていうのを強く思い知ったわけでしょう。",
        "secondary_evidence": "",
        "translated": "（旧译文：语义完全不同的一句）",
        "reasons": ["negation_missing"],
        "term_hints": {},
        "context_before": "",
        "context_after": "",
        "start": "00:15:38,800",
        "end": "00:15:44,839",
    }
    item.update(overrides)
    return item


def test_high_confidence_rewrite_not_rejected_as_excessive():
    """Round 1 错得离谱时，0.98 置信的正确修复不得被 excessive_rewrite 拒。"""
    item = _ja_item()
    decision = {
        "decision": "replace",
        "text": "强烈地意识到自己并不完美吧。会听到相当痛苦的话。",
        "confidence": 0.98,
        "reason": "canonical says 完璧ではない...思い知った (realized not perfect); round1 missed it",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert "excessive_rewrite" not in result["validation_reasons"], (
        result["validation_reasons"]
    )


def test_low_confidence_rewrite_still_rejected():
    """低置信大幅改写仍被拒绝（保守兜底不变）。"""
    item = _ja_item()
    decision = {
        "decision": "replace",
        "text": "强烈地意识到自己并不完美吧。会听到相当痛苦的话。",
        "confidence": 0.7,
        "reason": "rewrite",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert result["accepted"] is False
    assert "excessive_rewrite" in result["validation_reasons"]


def test_medium_confidence_rewrite_requires_evidence():
    """0.85-0.94 之间：无其他结构化证据时仍走 excessive_rewrite 检查。"""
    item = _ja_item()
    decision = {
        "decision": "replace",
        "text": "强烈地意识到自己并不完美吧。会听到相当痛苦的话。",
        "confidence": 0.9,
        "reason": "rewrite",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert "excessive_rewrite" in result["validation_reasons"]


def test_high_confidence_still_blocked_by_hallucinated_name_gate():
    """高置信不能绕过幻觉名防护（安全边界不放松）。"""
    item = _ja_item(reasons=["unknown_entity:ゼルカン"])
    decision = {
        "decision": "replace",
        "text": "泽尔卡恩来了",
        "confidence": 0.99,
        "reason": "invent a name",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert result["accepted"] is False
    assert any(
        reason.startswith("introduced_hallucinated_name")
        for reason in result["validation_reasons"]
    ), result["validation_reasons"]
