"""回归测试：EN 多 token ASR 变体（jin Gran → Jingran）可恢复 canonical。

真实素材/用户报告实证：`jin Gran` / `jin geon` / `jing ran` 是 Jingran
（景燃）的常见 ASR 听写变体；`Qing Xiao` / `Qing Qing Xiao` 是 Qingxiao
（清宵）的变体。当前 en_entity_normalize 的 _similarity_candidates 只
比较单个 ≥5 字母的 token，两 token 变体（"jin Gran"）不参与比较 →
无法 canonicalize → LLM 可能自由音译成"金格兰"等不存在的角色。

修复方向：相似度候选需支持 token 数变化的规范化串比较（去空白/标点/
大小写后按编辑距离与相似度匹配），仅唯一高置信候选自动应用。
"""
import pytest

from pipeline.preprocess.en_entity_normalize import normalize_en_entities


GLOSSARY = {
    "Jingran": "景燃",
    "Qingxiao": "清宵",
    "Phrolova": "弗洛洛",
}


def test_two_token_variant_jin_gran_normalized_to_jingran():
    result = normalize_en_entities("jin Gran is here", glossary=GLOSSARY)
    assert result.text == "Jingran is here", result.text
    assert result.canonical == ("Jingran",)
    assert result.confidence == "high"


def test_jin_geon_variant_normalized_to_jingran():
    result = normalize_en_entities("what about jin geon", glossary=GLOSSARY)
    assert result.text == "what about Jingran", result.text


def test_jing_ran_variant_normalized_to_jingran():
    result = normalize_en_entities("jing ran is back", glossary=GLOSSARY)
    assert result.text == "Jingran is back", result.text


def test_qing_xiao_two_token_variant_normalized_to_qingxiao():
    result = normalize_en_entities("Qing Xiao appears", glossary=GLOSSARY)
    assert result.text == "Qingxiao appears", result.text


def test_froolova_variant_normalized_to_phrolova():
    result = normalize_en_entities("Did Rover stab Froolova?", glossary=GLOSSARY)
    assert result.text == "Did Rover stab Phrolova?", result.text
    assert result.canonical == ("Phrolova",)
    assert result.confidence == "high"


def test_ambiguous_variant_not_forced():
    """多候选歧义（两个 canonical 都接近）时不得强行替换，保持原样。"""
    result = normalize_en_entities("Rico is here", glossary=GLOSSARY)
    assert result.text == "Rico is here"
