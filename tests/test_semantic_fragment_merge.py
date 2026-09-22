"""回归测试：日语滚动字幕切碎的残句单元必须修复（JA 错位根因）。

真实素材实证（2026-08-10 JA gl8vjOL1xWI）：
- id=76 canonical 以 `こその` 开头（`だからこそ` 被切碎）
- id=77 以 `だろうな` 开头（承接 id=76 的 `分かち合えるん`）
→ 每个语义单元都是残句，各自翻译造成 23 单元译文错位观感。

修复方向：语义单元合并时检测"残句开头"（接续助词/句尾承接片段），
与前一单元合并为完整句；有 `>>`/时间大间隔/speaker 边界时不合并。
"""
import pytest

from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.dedupe import coalesce_semantic_cues


def _subs(texts, gap_ms=0):
    """构造连续 cue（gap 可控，短时长避开 median 早退保护）。"""
    start = 0
    subs = []
    for i, text in enumerate(texts):
        end = start + 1000
        subs.append(Subtitle(
            i + 1,
            f"00:00:{start // 1000:02d},{start % 1000:03d}",
            f"00:00:{end // 1000:02d},{end % 1000:03d}",
            text,
        ))
        start = end + gap_ms
    return subs


def test_japanese_fragment_opening_is_merged_with_previous():
    """`こその`（だからこそ 的残片）开头的单元必须并入前一单元。

    真实素材（JA gl8vjOL1xWI id=76）canonical 以 `こその` 开头——
    完整句被滚动窗口切碎，残片独立成单元导致翻译错位。
    """
    subs = _subs([
        "完璧ではないっていうのを強く思い知ったわけでしょう。",
        "こその悩みっていうのはあるだろうね。",
    ])
    merged = coalesce_semantic_cues(subs, source_language="ja")
    assert len(merged) == 1, (
        f"残句开头未与前单元合并: {[s.text for s in merged]!r}"
    )
    assert "こそ" in merged[0].text


def test_japanese_sentence_tail_fragment_is_merged_with_previous():
    """`だろうな。` 这类承接上一句尾的片段并入前一单元。"""
    subs = _subs([
        "そこでこう共感というか分かち合えるん",
        "だろうな。だから日さんの葛藤というか",
    ])
    merged = coalesce_semantic_cues(subs, source_language="ja")
    assert len(merged) == 1
    assert "だろうな" in merged[0].text


def test_complete_sentences_are_not_merged():
    """完整句（以句号结尾）之间不强行合并。"""
    subs = _subs([
        "完璧ではないっていうのを強く思い知ったわけでしょう。",
        "結構苦しいことを聞きます。",
    ])
    merged = coalesce_semantic_cues(subs, source_language="ja")
    assert len(merged) == 2


def test_large_gap_prevents_fragment_merge():
    """时间大间隔（>2.5s）时残句也不合并——可能是独立短句或换人。"""
    subs = _subs([
        "そこでこう共感というか分かち合えるん",
        "だろうな。",
    ], gap_ms=3000)
    merged = coalesce_semantic_cues(subs, source_language="ja")
    assert len(merged) == 2


def test_ko_fragment_opening_also_merged():
    """韩语同样受益：助词开头的残句并入前一单元。"""
    subs = _subs([
        "그래서 그렇게 다가가서",
        "말하는 거예요.",
    ])
    merged = coalesce_semantic_cues(subs, source_language="ko")
    assert len(merged) == 1
    assert "말하는" in merged[0].text


def test_english_lowercase_rolling_fragment_is_merged_before_long_cue_early_exit():
    """A lowercase zero-gap continuation must not become a separate LLM ID."""
    subs = [
        Subtitle(
            1, "00:00:00,000", "00:00:03,000",
            ">> And people could grief you in co-op tower adversity.",
        ),
        Subtitle(
            2, "00:00:03,000", "00:00:06,000",
            "be so cool.",
        ),
        Subtitle(
            3, "00:00:06,000", "00:00:09,000",
            "That would be a new feature.",
        ),
    ]

    merged = coalesce_semantic_cues(subs, source_language="en")

    assert len(merged) == 2
    assert merged[0].text == (
        ">> And people could grief you in co-op tower adversity. be so cool."
    )
    assert merged[1].text == "That would be a new feature."
