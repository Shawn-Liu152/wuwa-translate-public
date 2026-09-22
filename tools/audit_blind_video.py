#!/usr/bin/env python3
"""Deterministic audit for a completed blind subtitle SRT."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms


RESIDUAL_PATTERNS = {
    "ja": re.compile(r"[\u3040-\u30ff]{2,}"),
    "ko": re.compile(r"[\uac00-\ud7af]{2,}"),
    "en": re.compile(r"[A-Za-z]{3,}"),
}
EN_ALLOW = {
    "DPS", "HP", "BOSS", "PV", "BGM", "OK", "NPC", "UI", "CPU",
}


def audit(lang: str, final_path: Path) -> dict:
    cues = parse_srt(str(final_path))
    overlap = 0
    nonpositive = 0
    residual = []
    trailing_full_stop = []
    split_versions = []
    normalised = []
    for index, cue in enumerate(cues):
        start = timestamp_to_ms(cue.start)
        end = timestamp_to_ms(cue.end)
        if end <= start:
            nonpositive += 1
        if index and timestamp_to_ms(cues[index - 1].end) > start:
            overlap += 1
        if cue.text.rstrip().endswith(("。", ".")):
            trailing_full_stop.append(cue.id)
        if lang == "en":
            words = RESIDUAL_PATTERNS[lang].findall(cue.text)
            if any(word.upper() not in EN_ALLOW for word in words):
                residual.append({"id": cue.id, "text": cue.text})
        elif RESIDUAL_PATTERNS[lang].search(cue.text):
            residual.append({"id": cue.id, "text": cue.text})
        normalised.append(re.sub(r"[\W_]+", "", cue.text))
        if index + 1 < len(cues):
            left = re.search(r"(\d+)\.$", cue.text.rstrip())
            right = re.match(r"^(\d+)(?!\d)", cues[index + 1].text.strip())
            if left and right:
                split_versions.append(f"{left.group(1)}.{right.group(1)}")

    adjacent_duplicates = []
    for index in range(1, len(cues)):
        if (
            len(normalised[index]) > 2
            and normalised[index] == normalised[index - 1]
        ):
            adjacent_duplicates.append({
                "ids": [cues[index - 1].id, cues[index].id],
                "text": cues[index].text,
            })
    return {
        "language": lang,
        "final": str(final_path.resolve()),
        "cues": len(cues),
        "overlap": overlap,
        "nonpositive_duration": nonpositive,
        "source_residual": len(residual),
        "source_residual_examples": residual[:20],
        "exact_duplicate_groups": len(adjacent_duplicates),
        "exact_duplicates": adjacent_duplicates,
        "trailing_full_stop": len(trailing_full_stop),
        "trailing_full_stop_ids": trailing_full_stop[:30],
        "split_version_numbers": split_versions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lang", choices=["en", "ja", "ko"])
    parser.add_argument("final", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.final.is_file():
        parser.error(f"SRT does not exist: {args.final}")
    report = audit(args.lang, args.final)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
