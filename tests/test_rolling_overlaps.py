"""YouTube 滚动式重叠字幕的修复与语义合并测试。

真实场景：YouTube 自动生成的韩语/日语字幕是"滚动窗口"结构——
相邻 cue 时间上大幅重叠（窗口滑动），但文本内容不重复（是上一行的延续）。
这类输入此前会导致：display-map 回填继承重叠时间轴 → 交付验证器拦截。

回归保护：test_rolling_overlaps_* 失败即代表真实素材验收链路损坏。
"""
from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms
from pipeline.preprocess.dedupe import (
    coalesce_semantic_cues,
    dedupe_youtube_subs,
    repair_rolling_overlaps,
)

# 摘自真实 YouTube 韩语自动字幕（명조 세계관 视频前 4 行）
ROLLING_SAMPLE = [
    ("명조는 멸망한 세계 이후를 배경으로", "00:00:05,160", "00:00:10,080"),
    ("하는 포스트 아포칼립스 세계관입니다", "00:00:07,720", "00:00:13,639"),
    ("과거 눈부신 발전을 이뤘던 인류", "00:00:10,080", "00:00:16,800"),
    ("문명에게 갑작스럽게 명이라는 재앙이", "00:00:13,639", "00:00:19,439"),
]


def _make(items):
    return [
        Subtitle(index, start, end, text)
        for index, (text, start, end) in enumerate(items, start=1)
    ]


def test_repair_rolling_overlaps_clips_to_non_overlapping_sequence():
    repaired = repair_rolling_overlaps(_make(ROLLING_SAMPLE))

    for prev, next_cue in zip(repaired, repaired[1:]):
        assert timestamp_to_ms(next_cue.start) >= timestamp_to_ms(prev.end), (
            f"仍存在时间重叠: #{prev.id} {prev.start}--{prev.end} 与 "
            f"#{next_cue.id} {next_cue.start}--{next_cue.end}"
        )
    # 文本与顺序保持完整
    assert [c.text for c in repaired] == [
        "명조는 멸망한 세계 이후를 배경으로",
        "하는 포스트 아포칼립스 세계관입니다",
        "과거 눈부신 발전을 이뤘던 인류",
        "문명에게 갑작스럽게 명이라는 재앙이",
    ]


def test_repair_rolling_overlaps_keeps_non_overlapping_cues_untouched():
    items = [
        ("첫 번째 완전한 문장입니다.", "00:00:01,000", "00:00:04,000"),
        ("두 번째 완전한 문장입니다.", "00:00:04,500", "00:00:08,000"),
    ]
    repaired = repair_rolling_overlaps(_make(items))
    assert [(c.start, c.end) for c in repaired] == [
        ("00:00:01,000", "00:00:04,000"),
        ("00:00:04,500", "00:00:08,000"),
    ]


def test_semantic_coalesce_merges_rolling_rows_into_complete_sentences():
    repaired = repair_rolling_overlaps(_make(ROLLING_SAMPLE))
    merged = coalesce_semantic_cues(repaired, source_language="ko")

    # 滚动行中位时长 > 2s 也不得早退：必须按终止形合并成完整句
    assert len(merged) == 2, [c.text for c in merged]
    assert merged[0].text == "명조는 멸망한 세계 이후를 배경으로 하는 포스트 아포칼립스 세계관입니다"
    assert merged[1].text == (
        "과거 눈부신 발전을 이뤘던 인류 문명에게 갑작스럽게 명이라는 재앙이"
    )
    # 合并后时间轴首尾相接、不重叠
    assert timestamp_to_ms(merged[1].start) >= timestamp_to_ms(merged[0].end)


def test_youtube_dedupe_preserves_high_similarity_negation_change():
    """相邻句只差否定词时不是同一滚动窗口，两个 source truth 都必须保留。"""
    subtitles = _make([
        ("그가 오늘 왔어", "00:00:01,000", "00:00:03,000"),
        ("그가 오늘 안 왔어", "00:00:03,100", "00:00:05,000"),
    ])

    deduped = dedupe_youtube_subs(subtitles, source_language="ko")

    assert [cue.text for cue in deduped] == [
        "그가 오늘 왔어",
        "그가 오늘 안 왔어",
    ]
