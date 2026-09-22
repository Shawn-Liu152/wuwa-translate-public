"""Local Whisper evidence collection for high-risk subtitle windows.

This module never changes the main English source or Chinese translation. It creates
additional evidence that a subsequent LLM review or human can evaluate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from pipeline.parser.srt_parser import Subtitle, ms_to_timestamp, timestamp_to_ms


@dataclass(frozen=True)
class AudioWindow:
    start_ms: int
    end_ms: int
    keys: Tuple[str, ...]


def build_windows(risk_items: Iterable[dict], padding_ms: int = 1500, merge_gap_ms: int = 1000) -> List[AudioWindow]:
    """Create bounded, merged windows from risk items without needing audio access."""
    raw = []
    for item in risk_items:
        start = max(0, timestamp_to_ms(item["start"]) - padding_ms)
        end = timestamp_to_ms(item["end"]) + padding_ms
        raw.append((start, end, item["key"]))
    raw.sort()

    windows: List[AudioWindow] = []
    for start, end, key in raw:
        if windows and start <= windows[-1].end_ms + merge_gap_ms:
            previous = windows[-1]
            windows[-1] = AudioWindow(previous.start_ms, max(previous.end_ms, end), previous.keys + (key,))
        else:
            windows.append(AudioWindow(start, end, (key,)))
    return windows


def remap_local_subtitles(subtitles: Iterable[Subtitle], offset_ms: int) -> List[Subtitle]:
    """Map subtitles produced for a clipped audio file back to source-video time."""
    remapped = []
    for index, subtitle in enumerate(subtitles, start=1):
        remapped.append(Subtitle(
            id=index,
            start=ms_to_timestamp(timestamp_to_ms(subtitle.start) + offset_ms),
            end=ms_to_timestamp(timestamp_to_ms(subtitle.end) + offset_ms),
            text=subtitle.text,
        ))
    return remapped
