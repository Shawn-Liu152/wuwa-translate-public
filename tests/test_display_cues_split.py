"""display_cues 回填切分质量测试：无标点源文本不得把词从中间切断。"""
import pytest

from pipeline.display_cues import _split_text


@pytest.mark.parametrize("desired", [2, 3, 4])
def test_split_text_prefers_space_boundaries_for_unpunctuated_korean(desired):
    text = "명조는 멸망한 세계 이후를 배경으로 하는 포스트 아포칼립스 세계관입니다"
    pieces = _split_text(text, desired)

    assert len(pieces) == desired
    # 实现把切点处的空格丢弃（每切一处丢一个）；join 补回后若与原文本
    # 完全相等，即证明：内容无丢失 + 每个切点都落在空格处（词未被切开）
    assert " ".join(pieces) == text, (
        f"切点不在空格处或内容丢失: {pieces!r}"
    )


def test_split_text_falls_back_to_character_split_for_space_free_cjk():
    # 纯 CJK 无空格文本（日语假名/汉字）只能字符切分，但不得抛错且内容完整
    text = "これは完全な日本語の文章で句読点がありません"
    pieces = _split_text(text, 3)

    assert len(pieces) == 3
    assert "".join(pieces) == text


def test_split_text_prefers_clause_boundaries_when_punctuation_exists():
    text = "鸣潮是以毁灭后的世界为背景的后末日世界观，过去闪耀的文明，遭遇了名为鸣的灾难。"
    pieces = _split_text(text, 2)

    assert len(pieces) == 2
    assert pieces[0].endswith("，") or pieces[0].endswith("。"), pieces[0]


def test_chinese_no_punctuation_splits_at_function_word_boundary():
    """中文无标点长文本不得切断音译专名（如"斯特赖德"）。

    真实素材（reaction 成品 #1-2）暴露："这是把斯特赖德之门剪开…"被切成
    "斯特赖|德"。修复：字符 fallback 优先在虚词（的/之/把/在/了）边界切。
    """
    from pipeline.display_cues import _split_text

    text = "这是把斯特赖德之门剪开"

    pieces = _split_text(text, 2)

    assert "".join(pieces) == text, "内容不得丢失"
    assert all(p.strip() for p in pieces)
    assert "斯特赖德" in pieces[0] or "斯特赖德" in pieces[1], "音译词不得被切断"
    # 切点应落在虚词"之"前：这是把斯特赖德 | 之门剪开
    assert pieces == ["这是把斯特赖德", "之门剪开"], pieces
