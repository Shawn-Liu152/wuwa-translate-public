"""回归测试：ASR 拉丁缩写变体（AMS）必须归一化到韩文标准形式。

真实素材实证（2026-08-10 KO p0bHZydkDrA）：union 中 4 处 `AMS`
（에이메스/爱弥斯 的拉丁缩写），asr_corrections.json 无此条目：
- 3 处靠模型上下文猜成"爱弥斯"
- 1 处（08:09）final 残留原文 "AMS一直叫我们回家" → source residual 违规

修复方向：asr_corrections.json 增加 AMS→에이메스，且 normalize_asr_text
对拉丁字母缩写变体也生效。
"""
import pytest

from pipeline.preprocess.asr_normalize import (
    load_asr_corrections, normalize_asr_text,
)


def test_ams_latin_abbreviation_normalized_to_korean_canonical():
    corrections = {
        "에이메스": "에이메스",
        "AMS": "에이메스",
        "게이드": "에이메스",
    }
    text = "방금 AMS를 보고 급하게 도망하던데요."
    normalized = normalize_asr_text(text, corrections)
    assert "에이메스" in normalized
    assert "AMS" not in normalized


def test_ams_case_insensitive():
    corrections = {"AMS": "에이메스"}
    assert "에이메스" in normalize_asr_text("ams가 나한테", corrections)


def test_ams_word_boundary_not_inside_other_words():
    corrections = {"AMS": "에이메스"}
    # "CAMERA" 之类含 AMS 的普通词不得误伤（按 token 边界处理）
    text = "카메라 AMS 카메라"
    normalized = normalize_asr_text(text, corrections)
    assert "에이메스" in normalized


def test_korean_variants_still_normalized():
    corrections = {"게이드": "에이메스", "에메스": "에이메스"}
    assert normalize_asr_text("게이드가 왔다", corrections) == "에이메스가 왔다"
