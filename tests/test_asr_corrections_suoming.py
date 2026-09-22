"""回归测试：EN 双源 ASR 变体 Stamina / saw min → Suoming。

2026-09-03 双源实证：YouTube ASR 与剪映字幕把同一角色 Suoming（锁暝，
glossary 已锁定）听成不同变体——
- union canonical 源文本 "both shin and Stamina"（"Stamina" 出现在
  final.zh.en.union.final.srt 第 3 条）；
- 剪映 "both shin and saw min"（capcut.en.srt 第 19 条，同一段）；
- YouTube ASR 同段听作 "Shin and Sin"。

asr_corrections_en.json 补入后，归一化应把变体恢复为 canonical Suoming，
否则 LLM 会按规则把 canonical 源文本音译出"斯坦米娜"等假角色。
"""
from pipeline.preprocess.en_entity_normalize import normalize_en_entities
from pipeline.preprocess.asr_normalize import normalize_asr_text, load_asr_corrections


GLOSSARY = {
    "Suoming": "锁暝",
    "Hsin": "心",
}


def test_stamina_variant_normalized_to_suoming():
    result = normalize_en_entities("both shin and Stamina", glossary=GLOSSARY)
    assert "Suoming" in result.text
    assert "Suoming" in result.canonical
    assert result.confidence == "high"


def test_saw_min_variant_normalized_to_suoming():
    result = normalize_en_entities("both shin and saw min", glossary=GLOSSARY)
    assert "Suoming" in result.text
    assert "Suoming" in result.canonical
    assert result.confidence == "high"


def test_stamina_variant_normalized_via_asr_normalize():
    corrections = load_asr_corrections(source_language="en")
    assert corrections["Stamina"] == "Suoming"
    assert corrections["saw min"] == "Suoming"
    text = normalize_asr_text("both shin and Stamina", corrections)
    assert "Suoming" in text


def test_common_word_stamina_not_touched_without_glossary_evidence():
    """无 Suoming 术语证据时，普通词 stamina 不得被强行替换。"""
    result = normalize_en_entities(
        "I lost my stamina running", glossary={"Jingran": "景燃"},
    )
    assert result.text == "I lost my stamina running"
    assert result.confidence == "low"
