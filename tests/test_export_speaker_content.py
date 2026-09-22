"""回归测试：最终发布层不得丢弃 `>>` 说话人分隔符后的内容。

真实素材实证（2026-08-10 KO p0bHZydkDrA）：round2 中 22 行含 `>>`，
其中 21 行的后段内容在 final.zh.srt 中被 clean_export_subtitles.clean_text
静默删除。例如：
  R2 : 你到底骗了多少姑娘？ >> 啊所以说，这家伙到处许愿...
  FIN: 你到底骗了多少姑娘？
`>>` 是 YouTube 自动字幕的双说话人分隔符，不是 "accumulated context"。
不同说话人的话必须全部保留（可保留 `>>` 或合并为一行，但不得丢弃）。
"""
import pytest

from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.dedupe import clean_export_subtitles


def _subs(texts):
    return [
        Subtitle(i + 1, "00:00:00,000", f"00:00:0{i+1},000", text)
        for i, text in enumerate(texts)
    ]


def test_clean_export_keeps_content_after_speaker_separator():
    """`>>` 后的第二说话人内容必须保留，不得被当 context 丢弃。"""
    subs = _subs(["你到底骗了多少姑娘？ >> 啊所以说，这家伙到处许愿"])
    cleaned = clean_export_subtitles(subs)
    assert len(cleaned) == 1
    assert "啊所以说" in cleaned[0].text, (
        f"`>>` 后段被丢弃: {cleaned[0].text!r}"
    )
    assert "到处许愿" in cleaned[0].text


def test_clean_export_keeps_multiple_speaker_segments():
    """多个 `>>` 分隔的多说话人内容全部保留。"""
    subs = _subs(["什么？ >> 啊，不是亲生的，是我养的。 >> 问题都解完了吗？"])
    cleaned = clean_export_subtitles(subs)
    assert len(cleaned) == 1
    assert "不是亲生的" in cleaned[0].text
    assert "问题都解完了吗" in cleaned[0].text


def test_clean_export_empty_first_segment_keeps_rest():
    """首段为空时（`>> xxx`），`>>` 后的内容不能被丢弃。"""
    subs = _subs([">> 嗯？什么？是我啊。"])
    cleaned = clean_export_subtitles(subs)
    assert len(cleaned) == 1
    assert "是我啊" in cleaned[0].text


def test_clean_export_single_speaker_no_separator_unchanged():
    """无 `>>` 的普通文本行为不变。"""
    subs = _subs(["嗯？什么？是我啊。"])
    cleaned = clean_export_subtitles(subs)
    assert len(cleaned) == 1
    assert cleaned[0].text == "嗯？什么？是我啊。"
