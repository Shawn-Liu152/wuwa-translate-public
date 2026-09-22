from pipeline.entity_resolution import resolve_entity_candidates


def test_korean_near_name_becomes_review_candidate_without_auto_replacement():
    candidates = resolve_entity_candidates(
        "오늘 카르테지아를 살펴봅시다",
        {"카르테시아": "卡提希娅", "피비": "菲比"},
        source_language="ko",
    )

    assert candidates[0]["surface"] == "카르테지아"
    assert candidates[0]["official_source"] == "카르테시아"
    assert candidates[0]["official_target"] == "卡提希娅"
    assert candidates[0]["requires_review"] is True
    assert candidates[0]["auto_replace"] is False


def test_short_or_weak_matches_do_not_create_name_candidates():
    assert resolve_entity_candidates(
        "이 장면은 좋아요", {"피비": "菲比"}, source_language="ko",
    ) == []


def test_english_known_entities_are_glossary_discoveries_not_risks():
    candidates = resolve_entity_candidates(
        "Jingran meets Qingxiao tonight",
        {"Jingran": "景燃", "Qingxiao": "清宵"},
        source_language="en",
    )

    assert [(item["canonical_entity"], item["canonical_target"]) for item in candidates] == [
        ("Jingran", "景燃"), ("Qingxiao", "清宵"),
    ]
    assert all(item["authority"] == "glossary" for item in candidates)
    assert all(item["review_required"] is False for item in candidates)


def test_english_unknown_entity_is_review_only_and_not_transliterated():
    candidates = resolve_entity_candidates(
        "I cannot believe Xyzzqz did that", {}, source_language="en",
    )

    assert candidates == [{
        "surface": "Xyzzqz",
        "official_source": "",
        "official_target": "",
        "canonical_entity": "Xyzzqz",
        "canonical_target": "",
        "authority": None,
        "classification": "possible_entity",
        "risk": "unknown_entity",
        "confidence": 0.5,
        "evidence": "english_title_case",
        "requires_review": True,
        "review_required": True,
        "auto_replace": False,
    }]


def test_english_common_capitalisation_and_technical_tokens_are_ignored():
    negatives = [
        "I love this", "DPS HP BOSS", "https://Example.com/Jingran",
        "This is a normal sentence", "My friend is here", "Hello there",
        "My brother and Mom are here",
        # Regression: ordinary sentence-initial verbs and mixed technical
        # tokens must not be flagged as entities (T7-C review fix).
        "Check HP and BOSS", "Look at that", "Get ready", "See you later",
        "Think about it", "Take this", "Make it work", "Watch out",
    ]
    for text in negatives:
        assert resolve_entity_candidates(text, {}, source_language="en") == []


def test_contextual_multiword_brand_precedes_single_word_glossary_match():
    candidates = resolve_entity_candidates(
        "This video is sponsored by Buff Buff. Check out Buff Buff today.",
        {"Buff": "增益"},
        source_language="en",
    )

    contextual = [
        item for item in candidates
        if item.get("classification") == "contextual_entity"
    ]
    assert len(contextual) == 1
    assert contextual[0]["surface"] == "Buff Buff"
    assert contextual[0]["authority"] == "source_context"
    assert contextual[0]["risk"] == "contextual_entity_unverified"
    assert contextual[0]["review_required"] is True
    assert not any(
        item.get("classification") == "known_entity"
        and item.get("surface", "").casefold() == "buff"
        for item in candidates
    )
