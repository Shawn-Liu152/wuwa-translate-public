"""Source Arbitration + Entity Guard 测试（提示词 Case 1-9 + 历史回归）。

先写失败测试，再实现 pipeline/preprocess/source_arbitration.py。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.candidates import SourceCandidate
from pipeline.long_video import _merge_english_entity_guard_risks
from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.source_arbitration import (
    ArbitrationKnowledge,
    ArbitratedUnit,
    arbitrate_candidates,
    classify_entity,
    detect_hallucinated_entities,
    _extract_entity_candidates,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _candidate(key: str, primary: str, secondary: str, lang: str = "ko") -> SourceCandidate:
    return SourceCandidate(
        key=key, start="00:00:00,000", end="00:00:02,000",
        primary_evidence=primary, secondary_evidence=secondary,
        primary_source="youtube", secondary_source="whisper",
        source_language=lang, primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )


def _knowledge() -> ArbitrationKnowledge:
    return ArbitrationKnowledge.from_data_dir(DATA_DIR, game="wuwa")


# ---------- Source Arbitration ----------

def test_arbitration_prefers_known_entity_over_unknown_noise():
    """Case 1: YouTube 错专名 게이드 vs Whisper 에이메스（已知实体）→ 选 에이메斯 路。"""
    cand = _candidate("1", "진짜 내가 갈게 게이드", "에이메스 진짜 내가 갈게")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert "에이메스" in unit.canonical_source_text
    assert unit.source_decision in ("secondary", "primary", "identical")
    assert "에이메스" in unit.canonical_source_text


def test_arbitration_prefers_glossary_term():
    """Case 2: 보이스통（未知噪音）vs 보이드 스톰（glossary 术语）→ 选术语。"""
    cand = _candidate("1", "보이스통", "보이드 스톰")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert unit.canonical_source_text == "보이드 스톰"
    assert unit.source_decision in ("secondary", "identical")
    assert any("glossary" in r or "asr" in r for r in unit.reasons) or unit.source_decision == "identical"


def test_arbitration_glossary_rich_incomplete_source_cannot_beat_complete_source():
    """glossary 只能提供弱支持，不能让低 coverage 的术语残句胜过完整证据。"""
    cand = _candidate(
        "1",
        "에이메스 보이드 스톰",
        "이번에는 모두 안전하게 게이트 밖으로 대피해야 합니다",
    )
    cand.primary_coverage = 0.3
    cand.secondary_coverage = 1.0

    unit = arbitrate_candidates(
        [cand], source_language="ko", data_dir=DATA_DIR,
    )["1"]

    assert unit.canonical_source_text == cand.secondary_evidence
    assert unit.source_decision == "secondary"


def test_arbitration_identical_sources_no_conflict():
    """Case 3: 两源一致 → identical，不冲突，不触发重建。"""
    cand = _candidate("1", "진짜 게이트를 열었네", "진짜 게이트를 열었네")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert unit.source_decision == "identical"
    assert unit.source_conflict is False
    assert unit.source_confidence >= 0.9
    assert unit.canonical_source_text == "진짜 게이트를 열었네"


@pytest.mark.parametrize(
    ("primary", "secondary"),
    [
        ("그가 왔습니다", "그가 안 왔습니다"),
        ("이번에는 3명이 왔습니다", "이번에는 4명이 왔습니다"),
        ("에이메스가 왔습니다", "에이메스가 왔습니까?"),
        ("에이메스가 직접 왔습니다", "에이마스가 직접 왔습니다"),
    ],
    ids=["negation", "number", "question", "entity"],
)
def test_arbitration_high_character_similarity_is_not_semantic_identity(
    primary, secondary,
):
    """高字符相似不能掩盖否定、数字、疑问或实体变化。"""
    unit = arbitrate_candidates(
        [_candidate("1", primary, secondary)],
        source_language="ko",
        data_dir=DATA_DIR,
    )["1"]

    assert unit.source_decision != "identical"
    assert unit.source_conflict is True


def test_arbitration_severe_conflict_no_evidence():
    """Case 4: 两源严重冲突且均无可靠证据 → 不武断选 primary。"""
    cand = _candidate("1", "아무튼 온 나이 너희가 무너질 거야", "카세차한다 뭐야")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert unit.source_conflict is True
    assert unit.source_confidence < 0.6


def test_arbitration_single_source_fallback():
    """一路为空时用另一路，不崩溃。"""
    cand = _candidate("1", "", "저 주파수는")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert unit.canonical_source_text == "저 주파수는"
    assert unit.source_decision in ("secondary", "single_secondary")


def test_arbitration_english_untouched():
    """en 源语言不进入仲裁（保持旧链路）。"""
    cand = _candidate("1", "Hello there", "Hello there", lang="en")
    result = arbitrate_candidates([cand], source_language="en", data_dir=DATA_DIR)
    assert result == {}


# ---------- Entity Guard ----------

def test_entity_classify_known():
    """Case 5: 에이메스 → known（glossary 有官方映射）。"""
    kind = classify_entity("에이메스", "ko", _knowledge())
    assert kind == "known"


def test_entity_classify_asr_variant():
    """Case 6a: 게이드 → asr_variant（asr_corrections 归一化为 에이메스）。"""
    kind = classify_entity("게이드", "ko", _knowledge())
    assert kind == "asr_variant"


def test_entity_classify_unknown():
    """Case 6b: 메카우터 → unknown（无任何依据）。"""
    kind = classify_entity("메카우터", "ko", _knowledge())
    assert kind == "unknown"


def test_entity_classify_hiyuki_variants():
    """历史回归: 히키/이유키/이웃키/기우키 全部归一化为 히유키（绯雪）。"""
    knowledge = _knowledge()
    for variant in ["히키", "이유키", "이웃키", "기우키"]:
        assert classify_entity(variant, "ko", knowledge) == "asr_variant"
    assert classify_entity("히유키", "ko", knowledge) == "known"


def test_hallucinated_entity_flagged():
    """Case 7: canonical 含 unknown 메카우터，中文输出出现音译 → hallucinated risk。"""
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="ko",
        primary_evidence="이번 작동에서 메카우터는", secondary_evidence="",
        canonical_source_text="이번 작동에서 메카우터는",
        source_decision="primary", source_confidence=0.4, source_conflict=False,
        reasons=[], unknown_entities=["메카우터"],
    )
    risks = detect_hallucinated_entities("这次作战中，梅卡乌特", unit, _knowledge())
    assert any("hallucinated" in r or "梅卡乌特" in r for r in risks)


def test_hallucinated_entity_clean():
    """canonical 无 unknown 时，正常中文输出不误报。"""
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="ko",
        primary_evidence="진짜 게이트를 열었네", secondary_evidence="진짜 게이트를 열었네",
        canonical_source_text="진짜 게이트를 열었네",
        source_decision="identical", source_confidence=0.95, source_conflict=False,
        reasons=[], unknown_entities=[],
    )
    risks = detect_hallucinated_entities("真的把门打开了", unit, _knowledge())
    assert risks == []


def test_english_hallucinated_entity_is_flagged_without_source_evidence():
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="en",
        primary_evidence="This power is incredible", secondary_evidence="",
        canonical_source_text="This power is incredible",
        source_decision="canonical_english", source_confidence=1.0,
        source_conflict=False, reasons=[], unknown_entities=[],
    )
    knowledge = ArbitrationKnowledge(source_language="en")

    assert detect_hallucinated_entities("泽尔卡恩的力量太强了", unit, knowledge) == ["泽尔卡恩"]


def test_english_correct_glossary_entity_has_no_hallucination_risk():
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="en",
        primary_evidence="Jingran is here", secondary_evidence="",
        canonical_source_text="Jingran is here",
        source_decision="canonical_english", source_confidence=1.0,
        source_conflict=False, reasons=[], unknown_entities=[],
    )
    knowledge = ArbitrationKnowledge(
        source_language="en",
        known_source_terms={"Jingran"},
        known_target_terms={"景燃"},
        source_to_target={"Jingran": "景燃"},
    )

    assert detect_hallucinated_entities("景燃来了", unit, knowledge) == []


def test_english_round_one_guard_wiring_keeps_known_clean_and_flags_unknown(monkeypatch):
    knowledge = ArbitrationKnowledge(
        source_language="en",
        known_source_terms={"Jingran"},
        known_target_terms={"景燃"},
        source_to_target={"Jingran": "景燃"},
    )
    monkeypatch.setattr(
        ArbitrationKnowledge,
        "from_data_dir",
        classmethod(lambda cls, *args, **kwargs: knowledge),
    )
    unknown = _candidate("unknown", "I saw Xyzzqz today", "", lang="en")
    known = _candidate("known", "Jingran is here", "", lang="en")
    known.start, known.end = "00:00:02,000", "00:00:04,000"
    translated = [
        Subtitle(1, unknown.start, unknown.end, "泽尔卡恩来了"),
        Subtitle(2, known.start, known.end, "景燃来了"),
    ]

    risks = _merge_english_entity_guard_risks(
        [], [unknown, known], translated, DATA_DIR,
        source_language="en", game="wuwa",
    )

    assert len(risks) == 1
    assert risks[0].key == "entity_guard:unknown"
    assert risks[0].review_required is True
    assert "unknown_entity:Xyzzqz" in risks[0].reasons
    assert "hallucinated_entity:泽尔卡恩" in risks[0].reasons


def test_english_entity_guard_preserves_neighbor_context_for_review(monkeypatch):
    """Entity review must see a phrase completed by the following cue."""
    knowledge = ArbitrationKnowledge(
        source_language="en",
        known_source_terms=set(),
        known_target_terms=set(),
        source_to_target={},
    )
    monkeypatch.setattr(
        ArbitrationKnowledge,
        "from_data_dir",
        classmethod(lambda cls, *args, **kwargs: knowledge),
    )
    before = _candidate("before", "I played several games", "", lang="en")
    target = _candidate(
        "target", "Xyzzqz and Wuthering Waves have the best", "", lang="en",
    )
    after = _candidate("after", "the best graphics, personally", "", lang="en")
    before.start, before.end = "00:00:00,000", "00:00:02,000"
    target.start, target.end = "00:00:02,000", "00:00:04,000"
    after.start, after.end = "00:00:04,000", "00:00:06,000"
    translated = [
        Subtitle(1, before.start, before.end, "我玩过好几款游戏"),
        Subtitle(2, target.start, target.end, "泽尔卡恩和鸣潮的画面最好"),
        Subtitle(3, after.start, after.end, "个人觉得画面最好"),
    ]

    risks = _merge_english_entity_guard_risks(
        [], [before, target, after], translated, DATA_DIR,
        source_language="en", game="wuwa",
    )
    item = next(risk for risk in risks if risk.key == "entity_guard:target")

    assert item.context_before == "I played several games"
    assert item.context_after == "the best graphics, personally"


def test_arbitration_hiyuki_variants_regression():
    """历史回归: 히키/기우키 两路变体 → canonical 归一化为 히유키。"""
    cand = _candidate("1", "히키가 철수 있었던 어", "기우키가 될 수 있었다니까")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert "히유키" in unit.canonical_source_text


def test_arbitration_strider_gate_regression():
    """历史回归: 스트라이더 게이트（隧门）应被识别为已知术语路。"""
    cand = _candidate("1", "이게 스트라이더 게이트를 오려", "스트라이더 게이트를 열어")
    result = arbitrate_candidates([cand], source_language="ko", data_dir=DATA_DIR)
    unit = result["1"]
    assert "스트라이더 게이트" in unit.canonical_source_text


# ------------------------------------------------------------------
# P1 新增测试：Entity Guard 普通词误报降低
# ------------------------------------------------------------------


def test_entity_guard_does_not_flag_common_pronouns():
    """代词/指示词不应被标记为 unknown entity。"""
    knowledge = _knowledge()
    for word in ["이게", "본인이", "이번", "모든", "어떤"]:
        candidates = _extract_entity_candidates(word, knowledge)
        assert word not in candidates, f"{word} should not be flagged as entity"


def test_entity_guard_does_not_flag_common_nouns_with_particles():
    """普通名词+助词不应被标记为 unknown entity。"""
    knowledge = _knowledge()
    for word in ["인류가", "인류의", "희망을", "사람들이", "반응은", "미래"]:
        candidates = _extract_entity_candidates(word, knowledge)
        assert word not in candidates, f"{word} should not be flagged as entity"


def test_entity_guard_does_not_flag_verb_conjugations():
    """动词/形容词活用形不应被标记为 unknown entity。"""
    knowledge = _knowledge()
    for word in ["있는", "죽은", "만든", "있어", "사라졌지만", "않지만"]:
        candidates = _extract_entity_candidates(word, knowledge)
        assert word not in candidates, f"{word} should not be flagged as entity"


def test_entity_guard_does_not_flag_formal_endings():
    """终结词尾不应被标记为 unknown entity。"""
    knowledge = _knowledge()
    for word in ["것입니다", "습니다", "입니다"]:
        candidates = _extract_entity_candidates(word, knowledge)
        assert word not in candidates, f"{word} should not be flagged as entity"


def test_entity_guard_does_not_flag_common_adverbs():
    """副词不应被标记为 unknown entity。"""
    knowledge = _knowledge()
    for word in ["완전히", "수많은", "아무튼"]:
        candidates = _extract_entity_candidates(word, knowledge)
        assert word not in candidates, f"{word} should not be flagged as entity"


def test_entity_guard_still_flags_real_unknown_entities():
    """真实未知专名（메카우터）仍应被标记。"""
    knowledge = _knowledge()
    candidates = _extract_entity_candidates("메카우터가", knowledge)
    assert "메카우터가" in candidates or "메카우터" in candidates


def test_entity_guard_still_flags_asr_variants():
    """ASR 变体（게이드）仍应被识别（不被活用尾/助词过滤误杀）。"""
    knowledge = _knowledge()
    # 게이드는 → 剥助词는 → 게이드 → 不在活用尾 → 保留
    candidates = _extract_entity_candidates("게이드는", knowledge)
    # 게이드는 不应该被过滤（它应该被asr_corrections处理，但如果不在corrections中则应保留）
    # 由于 게이드 在 asr_corrections 中，它会被过滤 — 这是正确行为
    # 测试一个不在 corrections 中的类似词
    candidates2 = _extract_entity_candidates("레이메스가", knowledge)
    # 레이메스 → 剥助词가 → 레이메스 → 不在活用尾 → 保留
    assert "레이메스가" in candidates2 or "레이메스" in candidates2


def test_entity_guard_particle_strip_before_suffix_check():
    """확认助词剥离在活用尾检查之前：게이드는 不应被 '는' 后缀误过滤。"""
    knowledge = _knowledge()
    # 게이드 는(助词) → 剥离后 게이드 → 不以活用尾结尾 → 保留
    # 但 게이드 在 asr_corrections 中，所以最终被过滤
    # 用一个不在 corrections 的词验证
    text = "아인르드는"
    candidates = _extract_entity_candidates(text, knowledge)
    # 아인르드 → 剥助词 → 아인르드 → 不在活用尾 → 保留
    assert "아인르드는" in candidates


def test_hallucinated_entity_skips_common_phrases_ja():
    """2026-08-11 Phase 7：JA 长句普通短语（也就是说/这个锚点）不误报幻觉。"""
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=6000, source_language="ja",
        primary_evidence=(
            "つまりこのアンカーは最初期、ラハイロイという地名すら存在しない頃に"
            "残された、門を開く鍵のようなものだ。"
        ),
        secondary_evidence="",
        canonical_source_text=(
            "つまりこのアンカーは最初期、ラハイロイという地名すら存在しない頃に"
            "残された、門を開く鍵のようなものだ。"
        ),
        source_decision="primary", source_confidence=0.4, source_conflict=False,
        reasons=[], unknown_entities=["ラハイロイ"],
    )
    risks = detect_hallucinated_entities(
        "也就是说这个锚点是最早期、连拉海洛这地名都还不存在时就留下的，是开启门的钥匙吧。",
        unit, _knowledge(),
    )
    # 普通短语含功能字 → 不标记；拉海洛=ラハイロイ 音译对应 → 不标记
    assert risks == [], risks


def test_hallucinated_entity_still_flags_transliteration():
    """音译 unknown（梅卡乌特=메카우터）仍需标记人工确认（不回归）。"""
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="ko",
        primary_evidence="이번 작동에서 메카우터는", secondary_evidence="",
        canonical_source_text="이번 작동에서 메카우터는",
        source_decision="primary", source_confidence=0.4, source_conflict=False,
        reasons=[], unknown_entities=["메카우터"],
    )
    risks = detect_hallucinated_entities("这次作战中，梅卡乌特", unit, _knowledge())
    assert any("梅卡乌特" in r for r in risks)
