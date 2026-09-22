# -*- coding: utf-8 -*-
"""Few-shot 示例注入测试。"""
import pytest

from pipeline.translate.prompt_builder import PromptBuilder


def _make_builder():
    return PromptBuilder("data/prompts/ko-zh-CN.txt", source_language="ko")


class _Sub:
    def __init__(self, text, _id=1):
        self.text = text
        self.id = _id


ENTRIES = [
    {
        "approved": True,
        "source": "이게 스트라이더 게이트를",
        "source_normalized": "이게 스트라이더 게이트를",
        "final": "这就是隧门",
    },
    {
        "approved": True,
        "source": "설마 뽑나요?",
        "source_normalized": "설마 뽑나요?",
        "final": "不会真要抽中吧？",
    },
    {
        "approved": True,
        "source": "아 진짜 존다 잘생겨왜 그냥",
        "source_normalized": "아 진짜 존다 잘생겨왜 그냥",
        "final": "啊，真的帅爆了",
    },
    {
        "approved": False,  # 未批准不注入
        "source": "미승인 예시",
        "source_normalized": "미승인 예시",
        "final": "不应出现",
    },
]


def test_few_shot_examples_injected_on_token_overlap():
    builder = _make_builder()
    # 批次含 "스트라이더"（与示例 1 重叠）
    prompt = builder.build([_Sub("이게 스트라이더 게이트를 오려")], translation_memory=ENTRIES)

    assert "正确翻译示例" in prompt
    assert "这就是隧门" in prompt


def test_few_shot_skips_unrelated_batch():
    builder = _make_builder()
    prompt = builder.build(
        [_Sub("완전히 다른 문장입니다")], translation_memory=ENTRIES,
    )
    assert "正确翻译示例" not in prompt


def test_few_shot_excludes_unapproved_entries():
    builder = _make_builder()
    prompt = builder.build(
        [_Sub("미승인 예시가 여기 있어요")], translation_memory=ENTRIES,
    )
    assert "不应出现" not in prompt


def test_exact_match_section_still_works():
    builder = _make_builder()
    prompt = builder.build(
        [_Sub("설마 뽑나요?")], translation_memory=ENTRIES,
    )
    assert "已人工批准的历史译法" in prompt
    assert "不会真要抽中吧？" in prompt


# --- English few-shot mechanism (T7-B) ---

EN_ENTRIES = [
    {
        "approved": True,
        "source": "This character is broken",
        "source_normalized": "this character is broken",
        "final": "这角色也太超模了",
    },
    {
        "approved": True,
        "source": "Jingran is so cool",
        "source_normalized": "jingran is so cool",
        "final": "景燃也太帅了",
    },
    {
        "approved": False,  # 未批准不注入
        "source": "Unapproved English example",
        "source_normalized": "unapproved english example",
        "final": "不应出现",
    },
]


def _make_en_builder():
    return PromptBuilder("data/prompts/en-zh-CN.txt", source_language="en")


def test_en_few_shot_injected_on_token_overlap():
    builder = _make_en_builder()
    # 批次含 "broken"（与 en 示例 1 重叠）
    prompt = builder.build(
        [_Sub("This character is broken right now")], translation_memory=EN_ENTRIES,
    )
    assert "正确翻译示例" in prompt
    assert "这角色也太超模了" in prompt


def test_en_few_shot_casefold_match_is_case_insensitive():
    builder = _make_en_builder()
    # 大小写不同（JINGRAN vs jingran）仍应命中（en 用 casefold 归一化）
    prompt = builder.build(
        [_Sub("JINGRAN is so cool")], translation_memory=EN_ENTRIES,
    )
    assert "景燃也太帅了" in prompt


def test_en_few_shot_excludes_unapproved():
    builder = _make_en_builder()
    prompt = builder.build(
        [_Sub("Unapproved English example here")], translation_memory=EN_ENTRIES,
    )
    assert "不应出现" not in prompt


def test_en_few_shot_skips_unrelated_batch():
    builder = _make_en_builder()
    prompt = builder.build(
        [_Sub("Completely unrelated sentence")], translation_memory=EN_ENTRIES,
    )
    assert "正确翻译示例" not in prompt


# --- 语言感知检索（2026-08-11，TM_AUDIT 修复） ---

JA_ENTRIES = [
    {
        "approved": True,
        "source": "ちょっと待って、今のやばくない？",
        "source_normalized": "ちょっと待って、今のやばくない？",
        "final": "等等，刚才那个也太离谱了吧？",
    },
    {
        "approved": True,
        "source": "エイメスは時間を巻き戻した",
        "source_normalized": "エイメスは時間を巻き戻した",
        "final": "爱弥斯把时间倒流了",
    },
]


def _make_ja_builder():
    return PromptBuilder("data/prompts/ja-zh-CN.txt", source_language="ja")


def test_ja_few_shot_bigram_hits_with_particle_variation():
    builder = _make_ja_builder()
    # 语尾/助词不同仍应命中（bigram 覆盖）
    prompt = builder.build(
        [_Sub("ちょっと待って、今のはやばくない？")], translation_memory=JA_ENTRIES,
    )
    assert "正确翻译示例" in prompt
    assert "等等，刚才那个也太离谱了吧？" in prompt


def test_ja_few_shot_uses_language_label_not_ko():
    builder = _make_ja_builder()
    prompt = builder.build(
        [_Sub("エイメスは時間を巻き戻した")], translation_memory=JA_ENTRIES,
    )
    assert "JA: エイメスは時間を巻き戻した" in prompt
    assert "KO:" not in prompt


def test_ko_few_shot_strips_particles_for_match():
    builder = _make_builder()
    # 스트라이더를（助词를）应命中库中 스트라이더
    prompt = builder.build(
        [_Sub("이게 스트라이더를 봤어")], translation_memory=ENTRIES,
    )
    assert "这就是隧门" in prompt


def test_few_shot_ranks_by_score_not_file_order():
    builder = _make_builder()
    high = {
        "approved": True,
        "source": "스트라이더 게이트를 열었어",
        "source_normalized": "스트라이더 게이트를 열었어",
        "final": "我打开了隧门",
    }
    entries = ENTRIES + [high]
    prompt = builder.build(
        [_Sub("스트라이더 게이트를 열었어")], translation_memory=entries,
    )
    # 精确重叠的示例应排在最前（第一个注入）
    assert prompt.index("我打开了隧门") < prompt.index("这就是隧门")


def test_few_shot_diagnostics_exposes_candidates_selection_scores_and_reason():
    builder = _make_ja_builder()

    diagnostics = builder.few_shot_diagnostics(
        [_Sub("ちょっと待って、今のはやばくない？")], JA_ENTRIES,
    )

    assert diagnostics["language"] == "ja"
    assert diagnostics["candidate_count"] >= 1
    assert diagnostics["selected_count"] >= 1
    assert diagnostics["selected"][0]["source"].startswith("ちょっと待って")
    assert 0 < diagnostics["selected"][0]["score"] <= 1
    assert diagnostics["selected"][0]["reason"] == "ja_character_bigram_overlap"


def test_few_shot_rejects_one_generic_token_overlap_from_a_long_example():
    builder = _make_builder()
    entries = [{
        "approved": True,
        "source": "아 진짜 존다 잘생겨왜 그냥",
        "final": "啊，真的帅爆了",
    }]

    diagnostics = builder.few_shot_diagnostics(
        [_Sub("아 진짜 다른 이야기를 하자")], entries,
    )

    assert diagnostics["selected"] == []


def test_ja_few_shot_rejects_two_generic_bigram_overlaps():
    builder = _make_ja_builder()
    entries = [{
        "approved": True,
        "source": "これが私の決意。後悔はしてない。",
        "final": "这就是我的决心，我不后悔。",
    }]

    diagnostics = builder.few_shot_diagnostics(
        [_Sub("これは別の話じゃない。")], entries,
    )

    assert diagnostics["selected"] == []


def test_en_few_shot_does_not_aggregate_weak_overlap_across_cues():
    builder = _make_en_builder()
    entries = [{
        "approved": True,
        "source": "the hero was ready",
        "final": "英雄准备好了",
    }]

    diagnostics = builder.few_shot_diagnostics(
        [_Sub("the answer changed"), _Sub("it was strange")], entries,
    )

    assert diagnostics["selected"] == []


def test_ja_few_shot_does_not_aggregate_bigrams_across_cues():
    builder = _make_ja_builder()
    entries = [{
        "approved": True,
        "source": "これは私の決意",
        "final": "这是我的决意",
    }]

    diagnostics = builder.few_shot_diagnostics(
        [_Sub("これは別"), _Sub("私の話")], entries,
    )

    assert diagnostics["selected"] == []
