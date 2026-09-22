"""Codex review Minor: standalone punctuation token index misalignment.

_window_tokens() drops standalone punctuation ('no ! idea' -> ['no','idea'])
while trimming uses raw split() (['no','!','idea']). If overlap n lands
past a standalone punctuation token, trimming cuts at the wrong raw index.
Reproduce and decide whether to fix.
"""
from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.dedupe import dedupe_youtube_subs


def test_en_standalone_punctuation_overlap_trimming():
    """prev 尾与 cur 头有独立标点 token 时，修剪不能留下重复词。"""
    # prev 尾部: "wait ! here"  cur 头部: "wait ! here we go"（2 词重叠 + 独立标点）
    subs = [
        Subtitle(1, "00:00:01,000", "00:00:05,000",
                 "Okay so we finally got here wait ! here"),
        Subtitle(2, "00:00:05,000", "00:00:08,000",
                 "wait ! here we go"),
    ]
    out = dedupe_youtube_subs(subs, source_language="en")
    assert len(out) == 2
    # 索引错位：raw split 是 ['wait','!','here','we','go']，window_tokens 是
    # ['wait','here','we','go']；overlap=2 时旧代码 cur_words[2:]='here we go'
    # 留下 'here' 重复；修复后应跳过 'wait ! here' 得 'we go'。
    tail = out[1].text.strip()
    assert tail == "we go", f"standalone-punct 修剪错位: {tail!r}"
