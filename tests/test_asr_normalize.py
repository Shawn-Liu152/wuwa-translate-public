# -*- coding: utf-8 -*-
"""ASR 归一化测试：变体替换、长词优先、无修正表兜底。"""
import pytest

from pipeline.preprocess.asr_normalize import load_asr_corrections, normalize_asr_text

CORRECTIONS = {
    "데미야": "데니아",
    "데니야": "데니아",
    "보이드스텀": "보이드 스톰",
    "보이드스": "보이드 스톰",
    "엑소스라이더": "엑소스트라이더",
}


def test_named_variants_are_normalized():
    assert normalize_asr_text("아 데미야 데미야 아니야?", CORRECTIONS) == "아 데니아 데니아 아니야?"
    assert normalize_asr_text("아 데니야 씨발년아", CORRECTIONS) == "아 데니아 씨발년아"
    assert normalize_asr_text("보이드스텀 안에서", CORRECTIONS) == "보이드 스톰 안에서"
    assert normalize_asr_text("엑소스라이더가 여기 있죠", CORRECTIONS) == "엑소스트라이더가 여기 있죠"


def test_longest_variant_wins():
    """长变体优先：보이드스텀 不能被 보이드스 先替换。"""
    text = "보이드스텀"
    assert normalize_asr_text(text, CORRECTIONS) == "보이드 스톰"


def test_unknown_text_unchanged():
    assert normalize_asr_text("안녕하세요 오늘은 좋은 날입니다", CORRECTIONS) == (
        "안녕하세요 오늘은 좋은 날입니다"
    )


def test_empty_and_none():
    assert normalize_asr_text("") == ""
    assert normalize_asr_text(None) is None


def test_load_corrections_from_disk():
    """真实修正表文件存在且含已确认变体。"""
    corrections = load_asr_corrections()
    assert corrections
    assert corrections.get("데미야") == "데니아"
    assert corrections.get("보이드스텀") == "보이드 스톰"
    # 变体按长度降序（长词优先替换）
    keys = list(corrections.keys())
    assert len(keys) == len(set(keys))


def test_language_scoped_corrections_keep_default_and_ja_ko_behavior():
    default = load_asr_corrections()
    assert load_asr_corrections(source_language="ja") == default
    assert load_asr_corrections(source_language="ko") == default
    assert "Jingrana" not in default


def test_english_corrections_are_loaded_from_separate_table():
    corrections = load_asr_corrections(source_language="en")
    # 2026-08-10 审计扩展：新增多 token ASR 变体（jin Gran/jin geon/
    # jing ran/Qing Xiao/Froolova/AMS），确保核心变体仍在表中。
    assert corrections["Chingxiao"] == "Qingxiao"
    assert corrections["Jingrana"] == "Jingran"
    assert corrections["Gingran"] == "Jingran"
    assert corrections["jin Gran"] == "Jingran"
    assert corrections["jin geon"] == "Jingran"
    assert corrections["jing ran"] == "Jingran"
    assert corrections["Qing Xiao"] == "Qingxiao"
    assert corrections["Froolova"] == "Phrolova"
    assert normalize_asr_text("Jingrana, Chingxiao, Gingran", corrections) == (
        "Jingran, Qingxiao, Jingran"
    )


def test_verified_final_blind_english_entity_variants_are_normalized():
    corrections = load_asr_corrections(source_language="en")
    source = (
        "Aidious met Yuno, Peep, Karo, Fuva, Flova, Pova, Froolo, "
        "and Galabbrina in Valerant"
    )

    assert normalize_asr_text(source, corrections) == (
        "Avidius met Iuno, Pero, Calcharo, Phrolova, Phrolova, "
        "Phrolova, Phrolova, and Galbrena in Valorant"
    )
