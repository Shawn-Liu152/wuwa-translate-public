"""Detect and remove non-speech music/SFX annotations."""
from __future__ import annotations

import os
import re
from typing import List, Set


_WRAPPED_MARKER = re.compile(
    r"^\s*[\[\(\{\uff08\u3010](.+?)[\]\)\}\uff09\u3011]\s*$"
)
_EDGE_PUNCTUATION = " \t\r\n.,;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f"
_NON_CONTENT_SYMBOLS = "\u266a\u266b\u266c\u2669\U0001f3b5\U0001f3b6-_~\u2014\u2013\u2026#<>"

# Wrapped labels can safely be removed inside a spoken sentence. Bare labels
# are removed only when they occupy the complete subtitle.
_CUE_LABEL = (
    r"(?:music(?:\s+(?:is\s+)?playing)?|bgm|instrumental|"
    r"background\s+music|intro\s+music|outro\s+music|"
    r"upbeat\s+music|dramatic\s+music|music\s+and\s+singing|"
    r"singing\s+and\s+music|singing|song|lyrics|chorus|verse|"
    r"applause|sound\s+effects?|"
    r"\u97f3\u4e50|\u914d\u4e50|\u97f3\u6548|\u638c\u58f0|\u6b4c\u58f0|"
    r"\u97f3\u697d|\u62cd\u624b|\u7b11|\u7b11\u3044|\u53eb\u3073\u58f0|"
    r"음악|배경\s*음악|박수|웃음|노래|효과음|한숨|헉\s*소리|숨소리|기침|탄성|비명|환호|함성|콧방귀|비웃음|울음|신음|고함|외침|중얼거림|휘파람)"
)
_INLINE_CUE_RE = re.compile(
    rf"[\[\(\{{\uff08\u3010]\s*{_CUE_LABEL}\s*"
    rf"[\]\)\}}\uff09\u3011]",
    re.IGNORECASE,
)
_MUSIC_SYMBOL_RE = re.compile(r"[\u266a\u266b\u266c\u2669\U0001f3b5\U0001f3b6]+")
_BARE_CUE_RE = re.compile(
    rf"^\s*{_CUE_LABEL}\s*[.!?,;:\u3002\uff01\uff1f]*\s*$",
    re.IGNORECASE,
)


def _normalise_exact(text: str) -> str:
    """Normalise a complete-row marker without changing sentence content."""
    return " ".join(text.strip(_EDGE_PUNCTUATION).casefold().split())


def _tidy_after_removal(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        line = re.sub(
            r"\s+([,.;:!?\u3002\uff0c\uff01\uff1f\uff1b\uff1a])",
            r"\1",
            line,
        )
        if line.strip(_EDGE_PUNCTUATION + _NON_CONTENT_SYMBOLS):
            lines.append(line)
    return "\n".join(lines).strip()


def strip_music_annotations(text: str) -> str:
    """Remove universal inline music/SFX cues while preserving spoken text."""
    if not text:
        return ""
    cleaned = _INLINE_CUE_RE.sub(" ", text)
    cleaned = _MUSIC_SYMBOL_RE.sub(" ", cleaned)
    if _BARE_CUE_RE.fullmatch(cleaned):
        return ""
    return _tidy_after_removal(cleaned)


class MusicDetector:
    """Case-insensitive music/SFX marker detector and cleaner."""

    def __init__(self, music_file: str):
        self._markers: List[str] = []
        self._exact_markers: Set[str] = set()
        self._inline_markers: List[str] = []

        with open(music_file, "r", encoding="utf-8") as source:
            for raw_line in source:
                marker = raw_line.strip()
                if not marker:
                    continue
                marker_folded = marker.casefold()
                self._markers.append(marker_folded)
                wrapped = _WRAPPED_MARKER.match(marker)
                if wrapped:
                    alias = _normalise_exact(wrapped.group(1))
                    if alias:
                        self._exact_markers.add(alias)
                    self._inline_markers.append(marker)
                elif not any(character.isalnum() for character in marker):
                    self._inline_markers.append(marker)

    def is_music(self, text: str) -> bool:
        """Return ``True`` when *text* contains a configured marker."""
        if not text:
            return False
        text_folded = text.casefold()
        if any(marker in text_folded for marker in self._markers):
            return True
        return _normalise_exact(text) in self._exact_markers

    def strip_markers(self, text: str) -> str:
        """Remove configured and universal cues, retaining spoken words."""
        if not text:
            return ""
        cleaned = text
        for marker in sorted(self._inline_markers, key=len, reverse=True):
            cleaned = re.sub(re.escape(marker), " ", cleaned, flags=re.IGNORECASE)
        if _normalise_exact(cleaned) in self._exact_markers:
            return ""
        return strip_music_annotations(cleaned)

    def is_pure_music(self, text: str) -> bool:
        """Return ``True`` when removing cues leaves no spoken content."""
        return bool(text and text.strip()) and not self.strip_markers(text)


def load_music_detector(data_dir: str | None = None) -> MusicDetector:
    """Load the detector from ``data/music.txt``."""
    if data_dir is None:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
        )
    return MusicDetector(os.path.join(data_dir, "music.txt"))
