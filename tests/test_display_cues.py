from pipeline.display_cues import (
    enforce_minimum_duration, enforce_reading_rhythm, redistribute_translation,
)
from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms


def test_complete_translation_is_redistributed_over_original_timeline():
    translated = Subtitle(
        1, "00:00:00,000", "00:00:12,000",
        "我甚至不知道她当时到底想做什么，不过那个决定真的很奇怪。",
    )
    spans = [
        {"start": "00:00:00,000", "end": "00:00:03,000"},
        {"start": "00:00:03,000", "end": "00:00:07,000"},
        {"start": "00:00:07,000", "end": "00:00:12,000"},
    ]

    cues = redistribute_translation(translated, spans)

    assert 2 <= len(cues) <= 3
    assert cues[0].start == "00:00:00,000"
    assert cues[-1].end == "00:00:12,000"
    assert "".join(item.text for item in cues) == translated.text
    assert all(item.start < item.end for item in cues)
    assert all(not item.text.startswith(("，", "。", "！", "？")) for item in cues)


def test_reading_rhythm_splits_only_long_dense_cues_and_is_idempotent():
    source = _cue(
        1, 0, 14_000,
        "这是一条持续时间过长而且内容非常密集的字幕，需要稳定拆成几条更容易阅读的字幕",
    )

    once = enforce_reading_rhythm([source], max_duration_ms=6_000)
    twice = enforce_reading_rhythm([
        Subtitle(cue.id, cue.start, cue.end, cue.text) for cue in once
    ], max_duration_ms=6_000)

    assert len(once) >= 3
    assert once[0].start == source.start
    assert once[-1].end == source.end
    assert "".join(cue.text for cue in once) == source.text
    assert max(_durations(once)) <= 6_000
    assert [
        (cue.id, cue.start, cue.end, cue.text) for cue in twice
    ] == [
        (cue.id, cue.start, cue.end, cue.text) for cue in once
    ]


def test_version_number_is_never_split_across_display_cues():
    """真实日语盲测曾把 ``3.1`` 发布成前条 ``3.``、后条 ``1剧情``。"""
    translated = Subtitle(
        2,
        "00:00:00,320",
        "00:00:06,679",
        "这次含剧透，3.1剧情重大抉择考察+感想合集反应视频，那就开始吧",
    )
    spans = [
        {"start": "00:00:00,320", "end": "00:00:03,679"},
        {"start": "00:00:03,680", "end": "00:00:06,679"},
    ]

    cues = redistribute_translation(translated, spans)

    assert "".join(item.text for item in cues) == translated.text
    assert all(not item.text.endswith("3.") for item in cues[:-1])
    assert all(not item.text.startswith("1剧情") for item in cues[1:])
    assert any("3.1" in item.text for item in cues)


# ---- P1：亚 300ms 闪帧 cue 兜底（enforce_minimum_duration）----
#
# redistribute_subtitles 的 <300ms 兜底把短片段并入“前一条”，但首条
# display cue 没有前一条可并，会漏出 10ms 闪帧 cue
# （2026-09-06 真实任务实测：'00:00:03,550 --> 00:00:03,560'）。


def _cue(index, start_ms, end_ms, text):
    from pipeline.parser.srt_parser import ms_to_timestamp

    return Subtitle(
        index, ms_to_timestamp(start_ms), ms_to_timestamp(end_ms), text,
    )


def _durations(cues):
    return [
        timestamp_to_ms(cue.end) - timestamp_to_ms(cue.start) for cue in cues
    ]


def test_first_short_cue_with_gap_is_extended():
    """首条 10ms cue 且后方有空隙：顺延到 start+1000ms 上限内，不越下一条。"""
    cues = enforce_minimum_duration([
        _cue(1, 3_550, 3_560, "[screaming]"),
        _cue(2, 5_000, 9_000, "大家好"),
    ])

    assert _durations(cues)[0] == 1_000
    # 不得越过下一条起点（validator 重叠红线）
    assert timestamp_to_ms(cues[0].end) < timestamp_to_ms(cues[1].start)


def test_extension_is_capped_by_next_start():
    """空隙不足 300ms 但够 100ms 时按空隙截断顺延，仍不产生重叠。"""
    cues = enforce_minimum_duration([
        _cue(1, 0, 10, "短"),
        _cue(2, 400, 4_000, "后续"),
    ])

    assert timestamp_to_ms(cues[0].end) == 399
    assert _durations(cues)[0] >= 300


def test_adjacent_short_cue_merges_forward_without_touching_next_window():
    """相邻无空隙 → 文本并入下一条，下一条时间窗保持不动（不产生新重叠）。"""
    cues = enforce_minimum_duration([
        _cue(1, 0, 10, "碎片"),
        _cue(2, 11, 4_000, "完整句"),
    ])

    assert len(cues) == 1
    assert cues[0].text == "碎片 完整句"
    assert cues[0].start == _cue(2, 11, 4_000, "").start
    assert cues[0].end == _cue(2, 11, 4_000, "").end


def test_run_of_short_cues_fully_resolved():
    """连续短 cue 串逐级前向归并，处理完无任何 <300ms 残留。"""
    cues = enforce_minimum_duration([
        _cue(1, 0, 10, "一"),
        _cue(2, 11, 20, "二"),
        _cue(3, 21, 30, "三"),
        _cue(4, 5_000, 9_000, "正常句"),
    ])

    assert len(cues) == 2
    assert cues[0].text == "一 二 三"
    assert min(_durations(cues)) >= 300
    assert cues[-1].text == "正常句"


def test_last_short_cue_is_extended_freely():
    """末条短 cue 无右邻 → 直接顺延到最小可读时长。"""
    cues = enforce_minimum_duration([_cue(1, 10_000, 10_010, "结尾")])

    assert len(cues) == 1
    assert _durations(cues)[0] >= 300


def test_enforce_minimum_duration_is_idempotent():
    """审校页每次保存都会重写 reviewed SRT：对已处理结果再跑一遍必须零改动。"""
    def build():
        return [
            _cue(1, 3_550, 3_560, "[screaming]"),
            _cue(2, 3_561, 6_550, "大家好"),
            _cue(3, 20_000, 20_100, "短"),
            _cue(4, 20_101, 24_000, "收尾"),
        ]

    def snapshot(cues):
        return [(c.id, c.start, c.end, c.text) for c in cues]

    once = enforce_minimum_duration(build())
    second_input = [
        Subtitle(c.id, c.start, c.end, c.text) for c in once
    ]
    twice = enforce_minimum_duration(second_input)

    assert snapshot(twice) == snapshot(once)
    assert min(_durations(once)) >= 300


def test_normal_cues_pass_through_unchanged():
    """全部合规的 cue 不做任何修改（顺延/合并都不应误伤）。"""
    source = [
        _cue(1, 0, 3_000, "第一句"),
        _cue(2, 3_000, 6_000, "第二句"),
    ]
    result = enforce_minimum_duration(
        [Subtitle(c.id, c.start, c.end, c.text) for c in source]
    )

    assert [(c.id, c.start, c.end, c.text) for c in result] == \
        [(c.id, c.start, c.end, c.text) for c in source]


def test_short_translation_stays_one_cue_even_when_source_was_fragmented():
    translated = Subtitle(4, "00:00:20,000", "00:00:23,000", "太漂亮了！")
    spans = [
        {"start": "00:00:20,000", "end": "00:00:21,000"},
        {"start": "00:00:21,000", "end": "00:00:22,000"},
        {"start": "00:00:22,000", "end": "00:00:23,000"},
    ]

    cues = redistribute_translation(translated, spans)

    assert [(item.start, item.end, item.text) for item in cues] == [
        ("00:00:20,000", "00:00:23,000", "太漂亮了！"),
    ]


def test_one_long_source_span_can_still_be_split_for_readability():
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


def test_redistribute_subtitles_never_produces_overlapping_display_cues():
    """双源证据边界交叉时，回填的显示 cue 不得互相重叠。

    真实素材（鸣潮 3 章 5 幕主播反应合集）暴露：YouTube 候选与 Whisper 候选
    时间重叠（同段落不同边界），回填后显示 cue 互相包含，交付验证器拦截。
    """
    from pipeline.display_cues import redistribute_subtitles

    subtitles = [
        Subtitle(1, "00:00:00,000", "00:00:04,120", "这"),
        Subtitle(2, "00:00:02,240", "00:00:10,080", "就是那个斯特赖德门"),
    ]
    mapping = {
        "1": [{"start": "00:00:00,000", "end": "00:00:04,120"}],
        "2": [{"start": "00:00:02,240", "end": "00:00:10,080"}],
    }

    cues = redistribute_subtitles(subtitles, mapping)

    for prev, current in zip(cues, cues[1:]):
        assert timestamp_to_ms(current.start) >= timestamp_to_ms(prev.end) - 1, (
            f"显示 cue 仍重叠: #{prev.id} {prev.start}--{prev.end} 与 "
            f"#{current.id} {current.start}--{current.end}"
        )


def test_clamped_1ms_cue_is_merged_into_previous_cue():
    """clamp 截断产生的 <300ms 废 cue 必须并入前一条，不得瞬时闪烁。

    真实素材残留：部分重叠的双源候选被 clamp 推到前一条 end+1ms 后
    时长不足 1ms（如"等等，等一下。"00:13:00,680-00:13:00,681）。
    """
    from pipeline.display_cues import redistribute_subtitles

    subtitles = [
        Subtitle(1, "00:12:54,320", "00:13:00,679", "啊，等一下。"),
        Subtitle(2, "00:12:55,000", "00:13:00,700", "等等，等一下。"),
        Subtitle(3, "00:13:00,700", "00:13:05,353", "长得超帅但看不到前面。"),
    ]
    mapping = {
        "1": [{"start": "00:12:54,320", "end": "00:13:00,679"}],
        "2": [{"start": "00:12:55,000", "end": "00:13:00,700"}],
        "3": [{"start": "00:13:00,700", "end": "00:13:05,353"}],
    }

    cues = redistribute_subtitles(subtitles, mapping)

    text = " ".join(c.text for c in cues)
    assert "等等，等一下。" in text, "被并入的文本不得丢失"
    assert all(
        timestamp_to_ms(c.end) - timestamp_to_ms(c.start) >= 300 or len(c.text) <= 4
        for c in cues
    ), "不得存在 <300ms 的非短文本废 cue"


def test_very_short_text_split_does_not_crash():
    """极短文本（1-2 字符）被拆分时不得 IndexError。

    真实场景：日语 E2E 任务中，语义单元 "啊"（1 字符）在
    display cue 重分配时 desired=2 导致 cut > len(text)，
    text[cut] 越界崩溃。修复：cut >= len(text) 时不索引。
    """
    from pipeline.display_cues import redistribute_subtitles

    # 模拟真实场景：短文本 + 多段 display map
    subtitles = [
        Subtitle(1, "00:00:14,079", "00:00:29,839", "啊"),
    ]
    mapping = {
        "1": [
            {"start": "00:00:14,079", "end": "00:00:16,120"},
            {"start": "00:00:16,120", "end": "00:00:25,539"},
            {"start": "00:00:25,539", "end": "00:00:29,839"},
        ],
    }
    # 不应崩溃
    cues = redistribute_subtitles(subtitles, mapping)
    assert len(cues) >= 1
    assert all(c.text for c in cues)


def test_single_char_with_high_desired_does_not_crash():
    """单字符文本 + 长 duration → desired 可能 >1，不得崩溃。"""
    translated = Subtitle(1, "00:00:00,000", "00:00:10,000", "啊")
    spans = [
        {"start": "00:00:00,000", "end": "00:00:03,000"},
        {"start": "00:00:03,000", "end": "00:00:07,000"},
        {"start": "00:00:07,000", "end": "00:00:10,000"},
    ]
    cues = redistribute_translation(translated, spans)
    assert len(cues) >= 1
    assert all(c.text for c in cues)
