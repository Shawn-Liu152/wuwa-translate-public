"""Deterministic language/content classification for source subtitle cues."""
from __future__ import annotations

from enum import Enum
import re
import unicodedata

from pipeline.preprocess.music import MusicDetector, load_music_detector


class CueClass(str, Enum):
    already_zh = "already_zh"
    english_reaction = "english_reaction"
    music_sfx = "music_sfx"
    mixed = "mixed"
    unknown = "unknown"

    # Upper-case aliases keep call sites readable without changing the public
    # lower-case enum names used by serialized classification results.
    ALREADY_ZH = already_zh
    ENGLISH_REACTION = english_reaction
    MUSIC_SFX = music_sfx
    MIXED = mixed
    UNKNOWN = unknown


def _is_cjk_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


_BRACKETED_MARKER_RE = re.compile(
    r"[\[\(\{\uff08\u3010][^\[\]\(\)\{\}\uff08\uff09\u3011]+[\]\)\}\uff09\u3011]"
)


def has_cjk_content(text: str) -> bool:
    """Return whether *text* carries any CJK ideograph (Chinese content)."""
    return any(_is_cjk_han(character) for character in str(text or ""))


def is_pure_sfx_annotation(text: str) -> bool:
    """Return whether *text* is only bracketed stage directions/SFX markers.

    YouTube 自动字幕与 Whisper 用 ``[screaming]``/``[laughter]`` 这类方括号
    标注非语音；它们不在 ``data/music.txt`` 标记表里，此前被当英语送去翻译
    （2026-09-06 真实任务实测）。整条 cue 只剩括号标注（无任何字母数字/
    汉字残留）时判为纯 SFX；句中标注不算（剥除后仍有口语）。
    """
    value = str(text or "")
    if not _BRACKETED_MARKER_RE.search(value):
        return False
    residue = _BRACKETED_MARKER_RE.sub(" ", value)
    return not any(character.isalnum() for character in residue)


def classify_cue(
    text: str,
    source_language: str = "en",
    music_detector: MusicDetector | None = None,
) -> CueClass:
    """Classify one cue without calling an LLM.

    Ratios use non-punctuation, non-whitespace characters as the denominator.
    The conservative thresholds intentionally leave ambiguous content for
    manual review instead of guessing that it is English.
    """
    del source_language  # Reserved for future language-specific thresholds.
    value = str(text or "")
    if not value.strip():
        return CueClass.UNKNOWN
    if all(
        character.isspace()
        or unicodedata.category(character).startswith("P")
        for character in value
    ):
        return CueClass.UNKNOWN

    detector = music_detector or load_music_detector()
    if detector.is_pure_music(value):
        return CueClass.MUSIC_SFX
    if is_pure_sfx_annotation(value):
        return CueClass.MUSIC_SFX

    content = [
        character for character in value
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "S"))
    ]
    if not content:
        return CueClass.UNKNOWN

    total = len(content)
    cjk_ratio = sum(_is_cjk_han(character) for character in content) / total
    latin_ratio = sum(
        "LATIN" in unicodedata.name(character, "") for character in content
    ) / total

    if cjk_ratio >= 0.60 and latin_ratio < 0.15:
        return CueClass.ALREADY_ZH
    if latin_ratio >= 0.60 and cjk_ratio < 0.15:
        return CueClass.ENGLISH_REACTION
    if cjk_ratio >= 0.15 and latin_ratio >= 0.15:
        return CueClass.MIXED
    return CueClass.UNKNOWN
