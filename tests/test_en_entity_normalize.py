from pipeline.preprocess.en_entity_normalize import normalize_en_entities


GLOSSARY = {
    "Jingran": "景燃",
    "Qingxiao": "清宵",
    "Xiangli Yao": "相里要",
}


def test_evidence_variants_are_automatically_normalized():
    result = normalize_en_entities(
        "Jingrana met Chingxiao and Gingran", glossary=GLOSSARY,
    )
    assert result.text == "Jingran met Qingxiao and Jingran"
    assert result.canonical == ("Jingran", "Qingxiao")
    assert result.confidence == "high"
    assert result.candidates == ()


def test_case_and_punctuation_forms_are_high_confidence():
    result = normalize_en_entities("jingran met Xiangli-Yao", glossary=GLOSSARY)
    assert result.text == "Jingran met Xiangli Yao"
    assert result.confidence == "high"


def test_unique_missing_initial_is_recovered_without_data_alias():
    result = normalize_en_entities("ingran is here", glossary=GLOSSARY)
    assert result.text == "Jingran is here"
    assert result.confidence == "high"
    assert result.reason == "unique-affix-stripping"


def test_unknown_name_is_not_rewritten_or_transliterated():
    result = normalize_en_entities("Xyzzqz is here", glossary=GLOSSARY)
    assert result.text == "Xyzzqz is here"
    assert result.canonical == ()
    assert result.confidence == "low"
    assert result.reason == "unknown"
    assert "景" not in result.text and "清" not in result.text


def test_word_boundaries_prevent_substring_matches():
    result = normalize_en_entities("NotJingran remains untouched", glossary=GLOSSARY)
    assert result.text == "NotJingran remains untouched"
    assert result.confidence == "low"


def test_similar_token_is_advisory_and_not_rewritten():
    # 2026-08-10 修复：唯一高置信相似候选（距离 ≤2 且无歧义）自动
    # canonicalize——Jingram→Jingran 是 m/n 听写混淆，必须恢复 canonical
    # 再翻译，否则 LLM 会自由音译出"金格拉姆"等假名。
    result = normalize_en_entities("Jingram is here", glossary=GLOSSARY)
    assert result.text == "Jingran is here"
    assert "Jingran" in result.canonical
    assert result.confidence == "high"


def test_ambiguous_similar_token_stays_advisory():
    # 歧义候选（两个 canonical 都接近）仍保持 advisory，不强行替换。
    result = normalize_en_entities("Xyzzqx is here", glossary=GLOSSARY)
    assert result.text == "Xyzzqx is here"
    assert result.confidence == "low"


def test_default_glossary_contains_required_canonical_entity():
    result = normalize_en_entities("Jingrana is here")
    assert result.text == "Jingran is here"
    assert "Jingran" in result.canonical
