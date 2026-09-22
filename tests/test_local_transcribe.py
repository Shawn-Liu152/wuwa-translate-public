import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.local_transcribe import build_windows, remap_local_subtitles
from pipeline.parser.srt_parser import Subtitle


def test_adjacent_risk_windows_merge():
    items = [
        {"key": "a", "start": "00:00:05,000", "end": "00:00:06,000"},
        {"key": "b", "start": "00:00:06,500", "end": "00:00:07,000"},
    ]
    windows = build_windows(items, padding_ms=1000, merge_gap_ms=1000)
    assert len(windows) == 1
    assert windows[0].start_ms == 4000
    assert windows[0].end_ms == 8000
    assert windows[0].keys == ("a", "b")


def test_local_timestamp_remap():
    local = [Subtitle(1, "00:00:01,000", "00:00:02,500", "evidence")]
    remapped = remap_local_subtitles(local, 10000)
    assert remapped[0].start == "00:00:11,000"
    assert remapped[0].end == "00:00:12,500"


if __name__ == "__main__":
    test_adjacent_risk_windows_merge()
    test_local_timestamp_remap()
    print("[PASS] local transcription tests")
