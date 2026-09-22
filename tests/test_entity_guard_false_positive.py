"""回归测试：EN Entity Guard 误报率校准（2026-08-10 审计 lWgwc_xNzrg）。

审计发现 22 个 unknown_entity 中 20 个是句首大写普通词误报
（God(3处)/Jesus/Bro/Like/Rome/Lily/Jim/Kirkland），挤占 Round 2 审查资源；
真实实体 Rulah（疑似 Rupa 露帕 ASR 变体）反而漏过。

判定标准：
- 误报 = 普通英文词（感叹/虚词/常见名词/常见人名）→ 不得产生 unknown_entity
- 真报 = glossary 外但大写且非词典词的真实体（人名/ASR 变体）→ 必须保留
"""
import pytest

from pipeline.entity_resolution import resolve_entity_candidates


def _risks(text: str) -> list[str]:
    return [
        item["surface"]
        for item in resolve_entity_candidates(text, {}, source_language="en")
        if item.get("classification") == "possible_entity"
    ]


@pytest.mark.parametrize("text", [
    "Oh my God",
    "Dude, Jesus",
    "Bro, what",
    "Like 10 bucks",
    "At least in Rome",
    "Hey, Jim!",
])
def test_ordinary_words_no_longer_flagged_as_unknown_entities(text):
    """普通英文词（感叹/虚词/常见人名）不得再产生 unknown_entity 风险。"""
    assert _risks(text) == [], (
        f"{text!r} 不应产生 unknown_entity，实际: {_risks(text)}"
    )


@pytest.mark.parametrize("text", ["Rulah", "Froolova", "Cartethyia"])
def test_real_unknown_entities_still_flagged(text):
    """glossary 外但大写且非词典词的真实体（人名/ASR 变体）必须保留。"""
    assert text in _risks(text), (
        f"{text!r} 应产生 unknown_entity，实际: {_risks(text)}"
    )


def test_mid_sentence_title_case_common_words_are_not_entities():
    """断句/标点造成的中句大写普通词不能挤占实体审查队列。"""
    text = "The crowd yelled Holy after Three, two, one."

    assert _risks(text) == []
