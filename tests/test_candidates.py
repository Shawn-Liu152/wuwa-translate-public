import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.candidates import (
    SourceCandidate, build_english_union, build_source_union,
    preferred_source_text, union_to_subtitles,
)
from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.dedupe import (
    clean_export_subtitles,
    coalesce_micro_cues,
    coalesce_semantic_cues,
)


def test_capcut_only_segment_is_preserved():
    capcut = [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "CapCut first"),
        Subtitle(2, "00:00:03,000", "00:00:05,000", "CapCut missing in Whisper"),
    ]
    whisper = [Subtitle(1, "00:00:00,100", "00:00:02,100", "Whisper first")]

    candidates = build_english_union(capcut, whisper)
    assert len(candidates) == 2
    assert candidates[1].capcut_en == "CapCut missing in Whisper"
    assert "whisper_missing" in candidates[1].flags
    assert union_to_subtitles(candidates)[1].text == "CapCut missing in Whisper"


def test_whisper_only_segment_is_preserved():
    capcut = [Subtitle(1, "00:00:00,000", "00:00:02,000", "CapCut first")]
    whisper = [
        Subtitle(1, "00:00:00,100", "00:00:02,100", "Whisper first"),
        Subtitle(2, "00:00:03,000", "00:00:05,000", "Whisper extra"),
    ]

    candidates = build_english_union(capcut, whisper)
    assert len(candidates) == 2
    assert candidates[1].whisper_en == "Whisper extra"
    assert "capcut_missing" in candidates[1].flags


def test_minor_asr_formatting_difference_is_not_a_conflict():
    capcut = [Subtitle(1, "00:00:00,000", "00:00:02,000", "Let's go, Rover!")]
    whisper = [Subtitle(1, "00:00:00,050", "00:00:02,050", "lets go rover")]

    candidates = build_english_union(capcut, whisper)

    assert "text_conflict" not in candidates[0].flags


def test_secondary_english_wins_when_capcut_is_chinese():
    capcut = [Subtitle(1, "00:00:00,000", "00:00:02,000", "这首歌真好听")]
    whisper = [Subtitle(1, "00:00:00,050", "00:00:02,050", "This song is amazing")]

    candidates = build_english_union(capcut, whisper)
    subtitles = union_to_subtitles(candidates)

    assert subtitles[0].text == "This song is amazing"
    assert "secondary_english_preferred" in candidates[0].flags


def test_chinese_only_segment_is_omitted_without_renumbering_following_ids():
    capcut = [
        Subtitle(1, "00:00:00,000", "00:00:01,000", "中文主持人"),
        Subtitle(2, "00:00:02,000", "00:00:03,000", "English host"),
    ]

    subtitles = union_to_subtitles(build_english_union(capcut, []))

    assert [(item.id, item.text) for item in subtitles] == [(2, "English host")]


def test_time_near_duplicate_secondary_is_not_appended_to_union():
    capcut = [
        Subtitle(
            1, "00:00:10,000", "00:00:12,000",
            "God damn she's so elegant",
        ),
    ]
    youtube = [
        Subtitle(
            1, "00:00:12,600", "00:00:12,610",
            "God damn, she's so elegant.",
        ),
        Subtitle(
            2, "00:00:30,000", "00:00:31,000",
            "God damn, she's so elegant.",
        ),
    ]

    subtitles = union_to_subtitles(build_english_union(capcut, youtube))

    assert [item.text for item in subtitles] == [
        "God damn she's so elegant",
        "God damn, she's so elegant.",
    ]


def test_japanese_union_preserves_both_conflicting_evidences_without_mixing():
    primary = [Subtitle(
        1, "00:00:00,000", "00:00:02,000", "カルテジアさん、見た？",
    )]
    secondary = [Subtitle(
        1, "00:00:00,100", "00:00:02,100", "フィービーさん、見た？",
    )]

    candidates = build_source_union(primary, secondary, source_language="ja")

    assert isinstance(candidates[0], SourceCandidate)
    assert candidates[0].primary_evidence == "カルテジアさん、見た？"
    assert candidates[0].secondary_evidence == "フィービーさん、見た？"
    assert candidates[0].source_language == "ja"
    assert "text_conflict" in candidates[0].flags
    assert preferred_source_text(candidates[0]) == "カルテジアさん、見た？"


def test_japanese_secondary_spanning_two_primary_units_is_evidence_not_duplicate():
    primary = [
        Subtitle(1, "00:00:08,133", "00:00:11,200", "この動画はおすすめではありません"),
        Subtitle(2, "00:00:11,800", "00:00:19,733", "注意してください"),
    ]
    secondary = [
        Subtitle(
            1, "00:00:10,150", "00:00:13,040",
            "この動画はおすすめする動画ではありません。注意点です",
        ),
    ]

    candidates = build_source_union(primary, secondary, source_language="ja")

    assert len(candidates) == 2
    assert [item.primary_evidence for item in candidates] == [
        "この動画はおすすめではありません", "注意してください",
    ]


def test_japanese_semantic_cues_reassemble_capcut_fragments_before_translation():
    fragments = [
        Subtitle(1, "00:00:00,333", "00:00:01,333", "皆さんこんにちは"),
        Subtitle(2, "00:00:01,900", "00:00:02,933", "今回は次世代"),
        Subtitle(3, "00:00:03,100", "00:00:03,900", "林檎"),
        Subtitle(4, "00:00:03,966", "00:00:04,766", "ってことで"),
        Subtitle(5, "00:00:04,933", "00:00:05,366", "鬼さ"),
        Subtitle(6, "00:00:05,366", "00:00:05,700", "水"),
        Subtitle(
            7, "00:00:05,700", "00:00:07,366",
            "水の組み合わせの紹介となります",
        ),
        Subtitle(8, "00:00:08,133", "00:00:09,200", "最初にこの動画は"),
        Subtitle(9, "00:00:09,300", "00:00:10,400", "引くことをお勧めする"),
        Subtitle(10, "00:00:10,400", "00:00:11,200", "動画ではありません"),
    ]

    grouped = coalesce_semantic_cues(fragments, source_language="ja")

    assert [(item.start, item.end, item.text) for item in grouped] == [
        ("00:00:00,333", "00:00:01,333", "皆さんこんにちは"),
        (
            "00:00:01,900", "00:00:07,366",
            "今回は次世代林檎ってことで鬼さ水水の組み合わせの紹介となります",
        ),
        (
            "00:00:08,133", "00:00:11,200",
            "最初にこの動画は引くことをお勧めする動画ではありません",
        ),
    ]


def test_korean_semantic_cues_keep_connective_endings_with_final_predicate():
    fragments = [
        Subtitle(1, "00:00:00,000", "00:00:01,000", "저는 이게 정말 좋은데"),
        Subtitle(2, "00:00:01,100", "00:00:02,000", "가격이 너무 비싸서"),
        Subtitle(3, "00:00:02,100", "00:00:03,300", "지금은 추천하지 않습니다."),
        Subtitle(4, "00:00:04,500", "00:00:05,500", "다음 장면을 볼게요."),
    ]

    grouped = coalesce_semantic_cues(fragments, source_language="ko")

    assert [(item.start, item.end, item.text) for item in grouped] == [
        (
            "00:00:00,000", "00:00:03,300",
            "저는 이게 정말 좋은데 가격이 너무 비싸서 지금은 추천하지 않습니다.",
        ),
        ("00:00:04,500", "00:00:05,500", "다음 장면을 볼게요."),
    ]


def test_korean_union_aggregates_multiple_secondary_cues_as_evidence():
    primary = [Subtitle(
        1, "00:00:00,000", "00:00:06,000",
        "저는 그녀가 뭘 하려는지 정말 모르겠어요.",
    )]
    secondary = [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "저는 정말"),
        Subtitle(2, "00:00:02,000", "00:00:04,000", "그녀가 뭘 하려는지"),
        Subtitle(3, "00:00:04,000", "00:00:06,000", "모르겠어요"),
    ]

    candidates = build_source_union(primary, secondary, source_language="ko")

    assert len(candidates) == 1
    assert candidates[0].primary_evidence.startswith("저는 그녀가")
    assert candidates[0].secondary_evidence == "저는 정말 그녀가 뭘 하려는지 모르겠어요"
    assert preferred_source_text(candidates[0]) == primary[0].text
    assert candidates[0].secondary_coverage == 1.0


def test_english_semantic_cues_translate_cross_cue_sentence_once():
    fragments = [
        Subtitle(101, "00:00:00,000", "00:00:01,500", "I don't even"),
        Subtitle(102, "00:00:01,500", "00:00:03,000", "know what she"),
        Subtitle(103, "00:00:03,000", "00:00:05,000", "was trying to do there."),
        Subtitle(104, "00:00:06,500", "00:00:08,000", "That was strange."),
    ]

    grouped = coalesce_semantic_cues(fragments, source_language="en")

    assert [(item.start, item.end, item.text) for item in grouped] == [
        (
            "00:00:00,000", "00:00:05,000",
            "I don't even know what she was trying to do there.",
        ),
        ("00:00:06,500", "00:00:08,000", "That was strange."),
    ]

def test_final_export_removes_censor_markers_and_merges_near_duplicates():
    subtitles = [
        Subtitle(1, "00:00:00,000", "00:00:02,000", r"你好 [\h__\h]"),
        Subtitle(2, "00:00:01,900", "00:00:03,000", "你好"),
        Subtitle(3, "00:00:04,000", "00:00:06,000", "沉重的雨意伴着雾气升起"),
        Subtitle(4, "00:00:05,900", "00:00:08,000", "沉重的雨意伴着雾气缓缓升起"),
        Subtitle(5, "00:00:09,000", "00:00:10,000", "[ __ ]"),
        Subtitle(6, "00:00:20,000", "00:00:21,000", "你好"),
        Subtitle(7, "00:00:21,000", "00:00:21,010", "十毫秒滚动残片"),
        Subtitle(
            8, "00:00:22,000", "00:00:23,000",
            "他们这是想刺杀她吗？ >> 已经领先一步了",
        ),
        Subtitle(
            9, "00:00:22,900", "00:00:24,000",
            "他们这是想刺杀她吗",
        ),
    ]

    cleaned = clean_export_subtitles(subtitles)

    assert [(item.id, item.start, item.end, item.text) for item in cleaned] == [
        (1, "00:00:00,000", "00:00:03,000", "你好"),
        (3, "00:00:04,000", "00:00:08,000", "沉重的雨意伴着雾气缓缓升起"),
        (
            6,
            "00:00:20,000",
            "00:00:21,010",
            "你好十毫秒滚动残片",
        ),
        # 2026-08-10 审计修复：`>>` 是说话人分隔符，后段台词必须保留
        # （此前被当 accumulated context 丢弃）。
        (
            8,
            "00:00:22,000",
            "00:00:24,000",
            "他们这是想刺杀她吗？ 已经领先一步了",
        ),
    ]


def test_final_export_merges_short_text_in_overlapping_rolling_window():
    subtitles = [
        Subtitle(1, "00:00:01,000", "00:00:02,000", "看看这玩意儿"),
        Subtitle(
            2,
            "00:00:01,700",
            "00:00:05,000",
            "他们全都是内部自己做的，兄弟。看看这玩意儿",
        ),
    ]

    cleaned = clean_export_subtitles(subtitles)

    assert len(cleaned) == 1
    assert cleaned[0].text == "他们全都是内部自己做的，兄弟。看看这玩意儿"


def test_micro_continuation_moves_out_of_the_next_rolling_window():
    merged = coalesce_micro_cues([
        Subtitle(
            37,
            "00:01:50,640",
            "00:01:53,630",
            "I am Suisui, your host for the Wanmin",
        ),
        Subtitle(
            38,
            "00:01:53,630",
            "00:01:53,640",
            "Exhibition.",
        ),
        Subtitle(
            39,
            "00:01:53,640",
            "00:01:55,630",
            "Exhibition.\n>> Y'all saying Suisui.",
        ),
    ])

    assert [(item.id, item.text) for item in merged] == [
        (
            37,
            "I am Suisui, your host for the Wanmin Exhibition.",
        ),
        (39, ">> Y'all saying Suisui."),
    ]


if __name__ == "__main__":
    test_capcut_only_segment_is_preserved()
    test_whisper_only_segment_is_preserved()
    print("[PASS] candidate union tests")


def test_korean_union_merges_fully_nested_secondary_candidate():
    """完全包含在另一个候选时间窗内的双源候选必须合并（证据并入外层）。

    真实素材（主播 reaction 合集）暴露：Whisper 转写 `잘 안 보여. 어디?`
    时间窗（01:28,840-01:30,040）完全包含在 YouTube 候选 `어디?`
    （01:26,920-01:30,040）内，被保留为独立语义单元，回填后产生
    1ms 废 cue。合并时更完整的转写应提升为主文本。
    """
    primary = [Subtitle(1, "00:01:26,920", "00:01:30,040", ">> 어디?")]
    secondary = [Subtitle(1, "00:01:28,840", "00:01:30,040", ">> 잘 안 보여. 어디?")]

    union = build_source_union(primary, secondary, source_language="ko")

    assert len(union) == 1, f"应合并为 1 个候选，实际 {len(union)}"
    assert "잘 안 보여" in union[0].primary_evidence, "更完整的转写应提升为主文本"
    assert union[0].start == "00:01:26,920", "保留外层时间窗起点"
    assert union[0].end == "00:01:30,040", "保留外层时间窗终点"


def test_ja_whisper_long_cue_not_broadcast_to_overlapping_primaries():
    """Holdout l9NCconvSNA 实证（2026-08-11）：Whisper 长 cue 覆盖多个
    primary 短 cue 时，combined 分支把同一 secondary 文本广播给所有
    重叠 primary → union 出现 4 条相同文本 → 翻译重复。

    修复：combined 分支只 join 未被之前 primary 消费的 secondary。
    """
    primary = [
        Subtitle(1, "00:00:00,160", "00:00:06,399",
                 "わあ、かっけえな。この建物ドキドキするな。"),
        Subtitle(2, "00:00:06,480", "00:00:07,839", "おお。"),
        Subtitle(3, "00:00:07,839", "00:00:10,920",
                 "あまりにも強そうな場所すぎる。"),
    ]
    # Whisper 一条长 cue 覆盖全部三个 primary（粗分段）
    secondary = [
        Subtitle(1, "00:00:00,000", "00:00:10,920",
                 "わーかっきいなこの建物ドキドキするなこんなとこ呼びつけられて"),
    ]

    union = build_source_union(primary, secondary, source_language="ja")

    # 第一条 primary 吸收 Whisper 证据；后续 primary 不得再收到同文本
    assert union[0].secondary_evidence != "", "第一条应吸收 Whisper 证据"
    for cand in union[1:]:
        assert cand.secondary_evidence == "", (
            f"secondary 被广播到 {cand.start} 候选: {cand.secondary_evidence[:30]}"
        )


def test_ja_multi_secondary_join_still_works():
    """多段 secondary 拼接场景不回归：primary 长 cue 由多条 secondary
    短 cue 覆盖时应全部 join（原有行为）。"""
    primary = [
        Subtitle(1, "00:00:00,000", "00:00:10,000",
                 "長い文。続きも長い。"),
    ]
    secondary = [
        Subtitle(1, "00:00:00,000", "00:00:05,000", "長い文。"),
        Subtitle(2, "00:00:05,000", "00:00:10,000", "続きも長い。"),
    ]

    union = build_source_union(primary, secondary, source_language="ja")

    assert len(union) == 1
    assert "長い文" in union[0].secondary_evidence
    assert "続きも長い" in union[0].secondary_evidence
