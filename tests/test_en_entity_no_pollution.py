"""回归测试：实体归一化不得把普通英文词污染成角色名。

真实素材实证（2026-08-10 EN lWgwc_xNzrg）：
- Whisper 原文 "It's so full over." 正确
- union/translate 输入变成 "It's so full Rover." ← _strip_attached_forms 的
  missing_initial 分支把 "over"(== "Rover"[1:]) 误判为 Rover 丢首字母
- 最终译文被带偏（角色名凭空出现）

修复：missing_initial 只对 surface >= 5 字符生效（canonical >= 6），
4 字符普通词（over/no/ever/very 等）不再触发。
"""
import pytest

from pipeline.preprocess.en_entity_normalize import normalize_en_entities


GLOSSARY = {
    "Jingran": "景燃",
    "Rover": "漂泊者",
    "Phrolova": "弗洛洛",
    "Qingxiao": "清宵",
}


def test_common_word_over_not_corrupted_to_rover():
    result = normalize_en_entities("It's so full over.", glossary=GLOSSARY)
    assert result.text == "It's so full over.", (
        f"普通词 over 被污染成角色名: {result.text!r}"
    )
    assert "Rover" not in result.text


def test_common_words_not_corrupted_by_missing_initial():
    """丢首字母恢复不得误伤 4 字符以内的普通英文词。"""
    for phrase in ("no way", "ever since", "very good", "over here"):
        result = normalize_en_entities(phrase, glossary=GLOSSARY)
        assert result.text == phrase, f"{phrase!r} -> {result.text!r}"


def test_real_missing_initial_still_recovered():
    """真正的丢首字母（ingran→Jingran，>=5 字符）仍恢复。"""
    result = normalize_en_entities("ingran is here", glossary=GLOSSARY)
    assert result.text == "Jingran is here"


def test_attached_form_stripping_still_works():
    """粘连形式（RoverX）剥离仍生效——大写上下文才触发。"""
    result = normalize_en_entities("RoverX is coming", glossary=GLOSSARY)
    assert result.text == "Rover is coming"


@pytest.mark.parametrize(
    "source",
    [
        "What's going on here?",
        "the story's going to continue",
        "I can't pronounce her name",
        "It's Rover.",
    ],
)
def test_grammar_fragments_are_not_canonicalized_as_character_names(source):
    """Contractions and possessives are grammar, not split ASR name evidence."""
    result = normalize_en_entities(source, glossary=GLOSSARY | {
        "Suoming": "锁暝",
        "Jiyan": "忌炎",
    })
    assert result.text == source
    assert result.canonical in {(), ("Rover",)}
