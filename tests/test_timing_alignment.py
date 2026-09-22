from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms
from pipeline.timing_alignment import apply_timing, retime_subtitles


def test_retime_subtitles_keeps_primary_text_and_uses_speech_timing():
    source = [
        Subtitle(
            1, "00:00:02,230", "00:00:03,919",
            "I'm Corey, a video game composer, and today we're",
        ),
        Subtitle(
            2, "00:00:03,919", "00:00:11,509",
            "going to listen to more music from Wuthering Waves.",
        ),
        Subtitle(
            3, "00:00:42,549", "00:01:03,525",
            "This feels like it could be epic. Is it going to be disturbing?",
        ),
    ]
    timing = [
        Subtitle(
            1, "00:00:00,000", "00:00:06,540",
            "I'm Corey, a video game composer, and today we're going to listen "
            "to more music from Wuthering Waves.",
        ),
        Subtitle(
            2, "00:00:40,140", "00:00:43,799",
            "This feels like it could be epic. Is it going to be disturbing?",
        ),
    ]

    aligned = retime_subtitles(source, timing)

    assert [(item.id, item.text) for item in aligned] == [
        (item.id, item.text) for item in source
    ]
    assert aligned[0].start == "00:00:00,000"
    assert aligned[1].end == "00:00:06,540"
    assert aligned[2].start == "00:00:40,140"
    assert aligned[2].end == "00:00:43,799"
    assert all(
        0 < timestamp_to_ms(item.end) - timestamp_to_ms(item.start) < 10_000
        for item in aligned
    )


def test_apply_timing_updates_translation_without_changing_text():
    source = [
        Subtitle(21, "00:00:02,230", "00:00:03,919", "source one"),
        Subtitle(22, "00:00:03,919", "00:00:11,509", "source two"),
    ]
    translation = [
        Subtitle(1, "00:00:02,230", "00:00:03,919", "我是游戏作曲家"),
        Subtitle(2, "00:00:02,230", "00:00:11,509", "今天来听鸣潮音乐"),
    ]
    aligned_source = [
        Subtitle(21, "00:00:00,000", "00:00:03,700", "source one"),
        Subtitle(22, "00:00:03,700", "00:00:06,540", "source two"),
    ]

    result = apply_timing(translation, source, aligned_source)

    assert [(item.start, item.end, item.text) for item in result] == [
        ("00:00:00,000", "00:00:03,700", "我是游戏作曲家"),
        ("00:00:00,000", "00:00:06,540", "今天来听鸣潮音乐"),
    ]


def test_retime_ignores_distant_common_words_and_fills_continuation():
    source = [
        Subtitle(
            10, "00:00:38,549", "00:00:42,549",
            "that represents her before. I know the sort of feel of it.",
        ),
        Subtitle(
            11, "00:00:42,549", "00:01:03,525",
            "This feels like it could be epic. Is it going to be disturbing?",
        ),
        Subtitle(
            12, "00:01:08,550", "00:01:19,030",
            "We are in the key of F minor.",
        ),
    ]
    timing = [
        Subtitle(
            1, "00:00:34,500", "00:00:39,420",
            "that represents her before. I know the sort of feel of it.",
        ),
        Subtitle(2, "00:00:59,539", "00:00:59,979", "This feels"),
        Subtitle(
            3, "00:01:05,540", "00:01:09,980",
            "We are in the key of F minor.",
        ),
    ]

    aligned = retime_subtitles(source, timing)

    assert aligned[0].end == "00:00:39,420"
    assert aligned[1].start == "00:00:39,420"
    assert timestamp_to_ms(aligned[1].end) < 45_000
    assert aligned[2].start == "00:01:05,540"


def test_retime_rejects_impossible_whisper_word_durations_and_caps_fallback():
    source = [
        Subtitle(1, "00:04:22,000", "00:04:27,000", "changed"),
        Subtitle(
            2, "00:05:50,000", "00:06:07,000",
            "Sorry, what happened there? Is this another phase of the music?",
        ),
    ]
    timing = [
        Subtitle(1, "00:04:22,779", "00:04:36,139", "changed"),
        Subtitle(2, "00:06:20,000", "00:06:21,000", "unrelated speech"),
    ]

    aligned = retime_subtitles(source, timing)

    first_duration = timestamp_to_ms(aligned[0].end) - timestamp_to_ms(aligned[0].start)
    second_duration = timestamp_to_ms(aligned[1].end) - timestamp_to_ms(aligned[1].start)
    assert 400 <= first_duration <= 1_000
    assert aligned[1].start == source[1].start
    assert second_duration <= 8_000


def test_retime_rejects_large_timestamp_jumps_and_stays_monotonic():
    source = [
        Subtitle(
            1, "00:10:40,900", "00:10:42,800",
            "people are not giving me much context",
        ),
        Subtitle(
            2, "00:10:42,800", "00:10:45,030",
            "I think this kind of thing is better",
        ),
    ]
    timing = [
        Subtitle(
            1, "00:10:57,460", "00:10:58,180",
            "people are not giving me much context",
        ),
    ]

    aligned = retime_subtitles(source, timing)

    assert aligned[0].start == source[0].start
    assert timestamp_to_ms(aligned[0].end) <= timestamp_to_ms(aligned[1].start)
