"""Source-truth regressions for rolling-caption deduplication."""

import pytest

from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms
from pipeline.preprocess.dedupe import (
    clean_export_subtitles,
    dedupe_youtube_subs,
    repair_rolling_overlaps,
)


def _subtitles(*texts: str, overlapping: bool = False) -> list[Subtitle]:
    rows = []
    for index, text in enumerate(texts, start=1):
        start_ms = (index - 1) * (1_000 if overlapping else 2_100)
        end_ms = start_ms + 2_000
        rows.append(Subtitle(
            index,
            f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d}",
            f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}",
            text,
        ))
    return rows


@pytest.mark.parametrize(
    ("source_language", "first", "second"),
    [
        ("en", "He came today", "He didn't come today"),
        ("ja", "彼は今日来た", "彼は今日来なかった"),
        ("ko", "그는 사과를 2개 샀어", "그는 사과를 3개 샀어"),
    ],
)
def test_youtube_dedupe_preserves_semantic_mutations(
    source_language: str,
    first: str,
    second: str,
):
    deduped = dedupe_youtube_subs(
        _subtitles(first, second),
        source_language=source_language,
    )

    assert [cue.text for cue in deduped] == [first, second]


def test_clean_export_preserves_negation_and_question_marker_changes():
    subtitles = _subtitles(
        "He came today",
        "He didn't come today",
        "Are you coming today",
        "Are you coming today?",
    )

    cleaned = clean_export_subtitles(subtitles)

    assert [cue.text for cue in cleaned] == [
        "He came today",
        "He didn't come today",
        "Are you coming today",
        "Are you coming today?",
    ]


def test_clean_export_keeps_the_safe_longer_prefix_superset():
    longer = "今天我来到这里，然后准备去找她"
    shorter = "今天我来到这里"

    cleaned = clean_export_subtitles(_subtitles(longer, shorter))

    assert [cue.text for cue in cleaned] == [longer]


def test_youtube_dedupe_still_collapses_true_rolling_continuation():
    continuation = "I think we should go now"

    deduped = dedupe_youtube_subs(
        _subtitles("I think we should", continuation),
        source_language="en",
    )

    assert [cue.text for cue in deduped] == [continuation]


def test_repair_overlap_treats_high_similarity_mutation_as_distinct_text():
    repaired = repair_rolling_overlaps(
        _subtitles("He came today", "He didn't come today", overlapping=True),
        source_language="en",
    )

    assert [cue.text for cue in repaired] == [
        "He came today",
        "He didn't come today",
    ]
    assert timestamp_to_ms(repaired[0].end) == timestamp_to_ms(repaired[1].start)
