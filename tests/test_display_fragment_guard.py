"""display_cues 残句/孤儿片段回归测试（2026-08-10 三语审计实证）。

审计发现 KO Job（download/2170758a3e78）final 出现大量 display 层残句：
'和负担啊。'、'的人生中，即'、'的她，如今…'；33s 超长 cue 被按 6s 硬切成
3+ 残片；<300ms 片段并入前一条后不延时导致时间轴错位
（display-map 段数与 final 段数不一致 57/143）。
"""
import pytest

from pipeline.display_cues import (
    _split_text,
    _visible_length,
    redistribute_subtitles,
    redistribute_translation,
)
from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms


# ── 1. 长文本不得产生 <4 字符的孤儿残片 ──────────────────────────────

@pytest.mark.parametrize("desired", [2, 3, 4, 5, 8, 10])
def test_split_text_long_text_never_produces_short_orphan_pieces(desired):
    """字符硬切不得产生 <4 可见字符的孤儿残片（如 '的'、'中，即'）。"""
    text = "在她们漫长的人生中，即将成为传说的她如今正背负着沉重的命运和负担啊。"

    pieces = _split_text(text, desired)

    assert "".join(pieces) == text, "切分不得丢失内容"
    assert all(_visible_length(piece) >= 4 for piece in pieces), (
        f"产生 <4 字符孤儿残片: {pieces!r} (desired={desired})"
    )


def test_split_text_merges_short_fragments_into_previous():
    """短残片必须并入前一片，而不是独立成孤儿 cue。"""
    text = "在她们漫长的人生中，即将成为传说的她如今正背负着沉重的命运和负担啊。"

    pieces = _split_text(text, 8)

    assert "".join(pieces) == text
    assert all(_visible_length(piece) >= 4 for piece in pieces), pieces
    assert not any(piece == "的" for piece in pieces), pieces


# ── 2. '和负担啊。' 类完整短句不被硬切 ───────────────────────────────

def test_short_complete_sentence_stays_whole():
    """'和负担啊。' 完整短句即使源 spans 多段/时长长也不得被切开。"""
    translated = Subtitle(1, "00:00:00,000", "00:00:10,000", "和负担啊。")
    spans = [
        {"start": "00:00:00,000", "end": "00:00:03,000"},
        {"start": "00:00:03,000", "end": "00:00:07,000"},
        {"start": "00:00:07,000", "end": "00:00:10,000"},
    ]

    cues = redistribute_translation(translated, spans)

    assert [(c.start, c.end, c.text) for c in cues] == [
        ("00:00:00,000", "00:00:10,000", "和负担啊。"),
    ]


def test_complete_short_sentence_not_isolated_or_cut_from_long_text():
    """长文本重分配后，'和负担啊。' 必须完整留在某一条 cue 内，
    且不得产生 '的人生中，即' 式孤儿残句。

    审计实证：33s 超长 cue 只有 1 个 source span 时被按 6s 硬切成 5-6 片，
    '和负担啊。' 与 '的人生中，即' 沦为孤儿残句。
    """
    text = "在她们漫长的人生中，即将成为传说的她如今正背负着沉重的命运和负担啊。"
    translated = Subtitle(1, "00:00:00,000", "00:00:33,000", text)
    spans = [{"start": "00:00:00,000", "end": "00:00:33,000"}]

    cues = redistribute_translation(translated, spans)

    joined = "".join(c.text for c in cues)
    assert joined == text, "内容不得丢失"
    assert any("和负担啊。" in c.text for c in cues), "完整短句被切开"
    assert not any(c.text.startswith("的人生中") for c in cues), (
        f"仍产生 '的人生中，即' 式孤儿残句: {[c.text for c in cues]!r}"
    )


# ── 3. 超长 cue 的 desired 受 source_spans 数量约束 ──────────────────

def test_super_long_cue_desired_capped_by_source_spans():
    """33s 超长 cue + 1 个 source span：切分数 <= len(spans)+1。"""
    text = "在她们漫长的人生中，即将成为传说的她如今正背负着沉重的命运和负担啊。"
    translated = Subtitle(1, "00:00:00,000", "00:00:33,000", text)
    spans = [{"start": "00:00:00,000", "end": "00:00:33,000"}]

    cues = redistribute_translation(translated, spans)

    assert len(cues) <= len(spans) + 1, (
        f"desired 不受 source_spans 约束: {len(cues)} 段 > {len(spans) + 1}"
    )


def test_desired_cap_still_allows_splitting_when_spans_allow():
    """spans 充足时仍按可读性切分（回归：单 span 超长 cue 也能切成 2 段）。"""
    translated = Subtitle(
        5, "00:00:30,000", "00:00:42,000",
        "这段话虽然只有一个原始时间片，但中文仍然需要按语义拆开。",
    )

    cues = redistribute_translation(translated, [{
        "start": translated.start, "end": translated.end,
    }])

    assert len(cues) >= 2
    assert all(
        timestamp_to_ms(item.end) - timestamp_to_ms(item.start) <= 6_000
        for item in cues
    )


# ── 4. <300ms 合并片段时间轴正确（m-03）──────────────────────────────

def test_merged_short_piece_extends_previous_cue_end():
    """<300ms 片段并入前一条后，前一条 end 必须延至被并入片段的实际结束时间。

    审计实证：并入后不延时 → display-map 段数与 final 段数不一致（57/143），
    时间轴出现缝隙。
    """
    subtitles = [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "第一句。"),
        Subtitle(2, "00:00:01,900", "00:00:02,100", "第二句。"),
        Subtitle(3, "00:00:02,100", "00:00:05,000", "第三句。"),
    ]
    mapping = {
        "1": [{"start": "00:00:00,000", "end": "00:00:02,000"}],
        "2": [{"start": "00:00:01,900", "end": "00:00:02,100"}],
        "3": [{"start": "00:00:02,100", "end": "00:00:05,000"}],
    }

    cues = redistribute_subtitles(subtitles, mapping)

    assert len(cues) == 2
    # 第一条的 end 覆盖被并入的第二条（不早于 00:00:02,100）
    assert timestamp_to_ms(cues[0].end) >= timestamp_to_ms("00:00:02,100"), (
        f"并入后未延时: {cues[0].end}"
    )
    assert "第二句。" in cues[0].text, "被并入的文本不得丢失"
    # 时间轴无缝隙/重叠
    for prev, current in zip(cues, cues[1:]):
        assert timestamp_to_ms(current.start) > timestamp_to_ms(prev.end), (
            f"时间轴错位: {prev.start}--{prev.end} 与 {current.start}--{current.end}"
        )
