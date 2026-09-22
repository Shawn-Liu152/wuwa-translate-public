"""Retiming helpers that preserve the primary subtitle text."""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from pipeline.parser.srt_parser import Subtitle, ms_to_timestamp, timestamp_to_ms


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def _timed_tokens(subtitles: list[Subtitle]) -> tuple[list[str], list[tuple[int, int]]]:
    words: list[str] = []
    timings: list[tuple[int, int]] = []
    for subtitle in subtitles:
        tokens = _tokens(subtitle.text)
        if not tokens:
            continue
        start = timestamp_to_ms(subtitle.start)
        end = timestamp_to_ms(subtitle.end)
        raw_duration = max(end - start, len(tokens))
        duration = max(
            len(tokens) * 120,
            min(raw_duration, max(800, len(tokens) * 800)),
        )
        for index, token in enumerate(tokens):
            token_start = start + round(duration * index / len(tokens))
            token_end = start + round(duration * (index + 1) / len(tokens))
            words.append(token)
            timings.append((token_start, token_end))
    return words, timings


def _dominant_timing_cluster(
    indexes: list[int], timings: list[tuple[int, int]], *, gap_ms: int = 2_500,
) -> list[int]:
    if not indexes:
        return []
    clusters = [[indexes[0]]]
    for index in indexes[1:]:
        previous = clusters[-1][-1]
        if timings[index][0] - timings[previous][1] > gap_ms:
            clusters.append([index])
        else:
            clusters[-1].append(index)
    return max(
        clusters,
        key=lambda cluster: (
            len(cluster),
            -(timings[cluster[-1]][1] - timings[cluster[0]][0]),
        ),
    )


def retime_subtitles(
    source: list[Subtitle],
    timing_reference: list[Subtitle],
    *,
    minimum_match_ratio: float = 0.35,
) -> list[Subtitle]:
    """Return primary text on a speech-derived timeline.

    Words are aligned monotonically across the complete transcript. The
    timing reference contributes only timestamps; text and IDs always come
    from *source*. Cues without enough matching words keep their source time.
    """
    source_words: list[str] = []
    owners: list[int] = []
    cue_word_counts: list[int] = []
    for cue_index, subtitle in enumerate(source):
        tokens = _tokens(subtitle.text)
        cue_word_counts.append(len(tokens))
        source_words.extend(tokens)
        owners.extend([cue_index] * len(tokens))

    timing_words, timing_values = _timed_tokens(timing_reference)
    if not source_words or not timing_words:
        return [Subtitle(item.id, item.start, item.end, item.text) for item in source]

    matched: list[list[int]] = [[] for _ in source]
    matcher = SequenceMatcher(None, source_words, timing_words, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            source_index = block.a + offset
            matched[owners[source_index]].append(block.b + offset)

    aligned: list[Subtitle] = []
    used_reference = [False] * len(source)
    for cue_index, subtitle in enumerate(source):
        timing_indexes = _dominant_timing_cluster(
            matched[cue_index], timing_values
        )
        required = max(
            1 if cue_word_counts[cue_index] < 4 else 2,
            math.ceil(cue_word_counts[cue_index] * minimum_match_ratio),
        )
        if len(timing_indexes) < required:
            aligned.append(Subtitle(
                subtitle.id, subtitle.start, subtitle.end, subtitle.text,
            ))
            continue
        start = timing_values[min(timing_indexes)][0]
        end = timing_values[max(timing_indexes)][1]
        original_start = timestamp_to_ms(subtitle.start)
        if end <= start or abs(start - original_start) > 8_000:
            aligned.append(Subtitle(
                subtitle.id, subtitle.start, subtitle.end, subtitle.text,
            ))
            continue
        aligned.append(Subtitle(
            subtitle.id,
            ms_to_timestamp(start),
            ms_to_timestamp(end),
            subtitle.text,
        ))
        used_reference[cue_index] = True

    for cue_index, subtitle in enumerate(source):
        if used_reference[cue_index] or cue_index == 0:
            continue
        original_duration = (
            timestamp_to_ms(subtitle.end) - timestamp_to_ms(subtitle.start)
        )
        source_gap = (
            timestamp_to_ms(subtitle.start)
            - timestamp_to_ms(source[cue_index - 1].end)
        )
        if (
            original_duration < 10_000
            or source_gap > 3_000
            or not used_reference[cue_index - 1]
        ):
            continue
        start = timestamp_to_ms(aligned[cue_index - 1].end)
        estimated_duration = min(
            8_000,
            max(800, round(cue_word_counts[cue_index] / 3 * 1_000)),
        )
        end = start + estimated_duration
        next_aligned = next(
            (
                timestamp_to_ms(aligned[index].start)
                for index in range(cue_index + 1, len(source))
                if used_reference[index]
            ),
            None,
        )
        if next_aligned is not None and next_aligned > start:
            end = min(end, next_aligned)
        if end > start:
            aligned[cue_index] = Subtitle(
                subtitle.id,
                ms_to_timestamp(start),
                ms_to_timestamp(end),
                subtitle.text,
            )
            used_reference[cue_index] = True

    for cue_index, subtitle in enumerate(aligned):
        start = timestamp_to_ms(subtitle.start)
        end = timestamp_to_ms(subtitle.end)
        if end - start < 10_000:
            continue
        estimated_duration = min(
            8_000,
            max(800, round(cue_word_counts[cue_index] / 2.5 * 1_000)),
        )
        capped_end = start + estimated_duration
        if cue_index + 1 < len(aligned):
            next_start = timestamp_to_ms(aligned[cue_index + 1].start)
            if next_start > start:
                capped_end = min(capped_end, next_start)
        aligned[cue_index] = Subtitle(
            subtitle.id,
            subtitle.start,
            ms_to_timestamp(capped_end),
            subtitle.text,
        )

    for cue_index in range(1, len(aligned)):
        previous_end = timestamp_to_ms(aligned[cue_index - 1].end)
        current = aligned[cue_index]
        current_start = timestamp_to_ms(current.start)
        if current_start >= previous_end:
            continue
        current_end = timestamp_to_ms(current.end)
        if current_end <= previous_end:
            duration = min(
                8_000,
                max(800, round(cue_word_counts[cue_index] / 2.5 * 1_000)),
            )
            current_end = previous_end + duration
        aligned[cue_index] = Subtitle(
            current.id,
            ms_to_timestamp(previous_end),
            ms_to_timestamp(current_end),
            current.text,
        )
    return aligned


def apply_timing(
    subtitles: list[Subtitle],
    source_reference: list[Subtitle],
    aligned_source: list[Subtitle],
) -> list[Subtitle]:
    """Copy aligned timing by original interval coverage, preserving text."""
    aligned_by_id = {item.id: item for item in aligned_source}
    reference_pairs = [
        (source, aligned_by_id[source.id])
        for source in source_reference
        if source.id in aligned_by_id
    ]
    result = []
    for item in subtitles:
        item_start = timestamp_to_ms(item.start)
        item_end = timestamp_to_ms(item.end)
        covered = [
            aligned
            for source, aligned in reference_pairs
            if timestamp_to_ms(source.end) > item_start
            and timestamp_to_ms(source.start) < item_end
        ]
        if not covered:
            result.append(Subtitle(item.id, item.start, item.end, item.text))
            continue
        start = min(timestamp_to_ms(source.start) for source in covered)
        end = max(timestamp_to_ms(source.end) for source in covered)
        result.append(Subtitle(
            item.id,
            ms_to_timestamp(start),
            ms_to_timestamp(end),
            item.text,
        ))
    return result
