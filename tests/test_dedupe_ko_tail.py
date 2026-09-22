"""KO rolling-tail duplicate regression (holdout-a8ZSJPGGbbg case).

B is a punctuation-variant tail superset of A (adjacent windows). Step2
head-tail overlap used raw split() -> '여친입니다.' != '여친입니다' missed
the overlap -> duplicate survived into final subtitles.
"""
from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.dedupe import dedupe_youtube_subs


def test_ko_tail_superset_with_punctuation_variants_is_deduped():
    """A 长句尾部含 B 内容 + B 标点变体 → B 应被去重（tail duplicate = 0）。"""
    subs = [
        Subtitle(1, "00:12:10,560", "00:12:18,440",
                 "치사랑 봉링은 다음 공방에서 보여줄 건가 봐 근데 봉링이 생각보다 "
                 "되게 느리게 공개가 되네요 치사 제 여친입니다 건들지 마세요"),
        Subtitle(2, "00:12:18,440", "00:12:21,920",
                 "치사, 제 여친입니다. 건들지 마세요."),
    ]
    out = dedupe_youtube_subs(subs, source_language="ko")
    assert len(out) == 1, f"tail duplicate 未被去重: {len(out)} cues"
    assert "여친입니다" in out[0].text


def test_en_tail_overlap_with_punctuation_still_works():
    """EN 尾首重叠（标点变体）不回归（≥2 词重叠才修剪）。"""
    subs = [
        Subtitle(1, "00:00:01,000", "00:00:05,000",
                 "Okay so we finally got here, folks"),
        Subtitle(2, "00:00:05,000", "00:00:08,000",
                 "here, folks, and it's amazing!"),
    ]
    out = dedupe_youtube_subs(subs, source_language="en")
    # here 与 here, folks 与 folks, 标点变体归一化后应匹配 → 修剪重复
    assert len(out) == 2
    assert out[1].text.strip().startswith("and"), f"尾首重叠未修剪: {out[1].text}"


def test_ko_normal_words_not_over_merged():
    """普通 KO 句（共享词但不同内容）不被误合并（B-01 保护）。"""
    subs = [
        Subtitle(1, "00:00:01,000", "00:00:05,000",
                 "오늘 여기에 와서 그녀를 찾으러 갈 준비를 했어"),
        Subtitle(2, "00:00:05,000", "00:00:08,000",
                 "오늘 여기에 왔어"),
    ]
    out = dedupe_youtube_subs(subs, source_language="ko")
    assert len(out) == 2, "高共享前缀 ≠ 完全相同，不得合并（B-01）"
