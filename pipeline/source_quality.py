"""Deterministic, explainable quality checks for multilingual subtitle sources."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms
from pipeline.languages import normalize_language_pair


@dataclass(frozen=True)
class SourceQuality:
    usable: bool
    score: int
    subtitle_count: int
    language_ratio: float
    unique_ratio: float
    monotonic_ratio: float
    reasons: tuple[str, ...]
    micro_cue_ratio: float = 0.0
    fragment_ratio: float = 0.0
    needs_secondary: bool = False
    long_cue_ratio: float = 0.0
    needs_timing_alignment: bool = False
    source_language: str = "en"
    short_cue_ratio: float = 0.0
    hallucination_loop_ratio: float = 0.0
    semantic_unit_count: int = 0
    semantic_compression_ratio: float = 0.0
    entity_match_ratio: float = 1.0

    def to_dict(self) -> dict:
        values = asdict(self)
        # Old jobs and integrations used this name. Keep it on disk while all
        # new code consumes the language-neutral field.
        values["english_ratio"] = self.language_ratio
        return values

    @property
    def english_ratio(self) -> float:
        """Backward-compatible alias for records created before multilingual support."""
        return self.language_ratio


def assess_source_srt(
    path: str | Path, source_language: str = "en",
    expected_terms: list[str] | tuple[str, ...] | None = None,
) -> SourceQuality:
    """Rate whether an SRT is useful as independent declared-language evidence."""
    source_language = normalize_language_pair({
        "source_language": source_language,
    })["source_language"]
    target = Path(path)
    if not target.exists() or target.stat().st_size == 0:
        return SourceQuality(False, 0, 0, 0.0, 0.0, 0.0, ("missing",), source_language=source_language)
    try:
        subtitles = parse_srt(str(target))
    except (OSError, ValueError):
        return SourceQuality(False, 0, 0, 0.0, 0.0, 0.0, ("invalid_srt",), source_language=source_language)
    if not subtitles:
        return SourceQuality(False, 0, 0, 0.0, 0.0, 0.0, ("empty",), source_language=source_language)

    text = " ".join(item.text for item in subtitles)
    latin = len(re.findall(r"[A-Za-z]", text))
    non_latin = len(
        re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
    )
    if source_language == "en":
        language_ratio = latin / max(latin + non_latin, 1)
    elif source_language == "ja":
        japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
        meaningful = len(re.findall(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
        language_ratio = japanese / max(meaningful, 1)
    else:
        korean = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", text))
        meaningful = len(re.findall(
            r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]",
            text,
        ))
        language_ratio = korean / max(meaningful, 1)

    if source_language == "en":
        normalised = [
            " ".join(re.findall(r"[a-z0-9']+", item.text.lower()))
            for item in subtitles
        ]
    else:
        normalised = [
            re.sub(
                r"[\s\W_]+", "",
                unicodedata.normalize("NFKC", item.text),
            )
            for item in subtitles
        ]
    normalised = [item for item in normalised if item]
    unique_ratio = len(set(normalised)) / max(len(normalised), 1)

    monotonic = 0
    previous_start = -1
    for subtitle in subtitles:
        start = timestamp_to_ms(subtitle.start)
        end = timestamp_to_ms(subtitle.end)
        if start >= previous_start and end > start:
            monotonic += 1
        previous_start = start
    monotonic_ratio = monotonic / len(subtitles)
    micro_cue_ratio = sum(
        1
        for subtitle in subtitles
        if timestamp_to_ms(subtitle.end) - timestamp_to_ms(subtitle.start) <= 50
    ) / len(subtitles)
    short_cue_ratio = sum(
        1 for subtitle in subtitles
        if 200 <= timestamp_to_ms(subtitle.end) - timestamp_to_ms(subtitle.start) <= 1000
    ) / len(subtitles)
    long_cue_ratio = sum(
        1
        for subtitle in subtitles
        if timestamp_to_ms(subtitle.end) - timestamp_to_ms(subtitle.start) >= 10_000
    ) / len(subtitles)
    fragment_end = (
        re.compile(
            r"\b(?:a|an|the|for|or|and|but|to|of|with|because|if|when|"
            r"which|who|whose|is|are|was|were|has|have|had|do|does|did|"
            r"my|your|our|their)\s*[,:;-]*$",
            re.IGNORECASE,
        )
        if source_language == "en" else
        re.compile(r"(?:[、，]|(?:て|で|が|を|に|は|と|も|や|の|から|けど|し))$")
        if source_language == "ja" else
        re.compile(r"(?:고|는데|지만|면|니까|거든|아서|어서)$")
    )
    fragment_ratio = sum(
        1
        for subtitle in subtitles
        if fragment_end.search(
            re.sub(r"(?:>>+|\[[^\]]+\])", " ", subtitle.text).strip()
        )
    ) / len(subtitles)
    counts = {item: normalised.count(item) for item in set(normalised)}
    # A repeated one/two-character name can be legitimate dialogue evidence.
    # Hallucination loops are repeated phrase-sized emissions, typically over
    # dense short cues or silence.
    loop_count = sum(
        count for item, count in counts.items() if count >= 3 and len(item) >= 4
    )
    hallucination_loop_ratio = loop_count / max(len(normalised), 1)
    average_text_length = sum(map(len, normalised)) / max(len(normalised), 1)
    rapid_short_cues = short_cue_ratio >= 0.5 and average_text_length >= 4
    semantic_unit_count = len(subtitles)
    semantic_compression_ratio = 0.0
    if source_language in {"en", "ja", "ko"} and average_text_length >= 4:
        from pipeline.preprocess.dedupe import coalesce_semantic_cues
        semantic_unit_count = len(coalesce_semantic_cues(
            subtitles, source_language=source_language,
        ))
        semantic_compression_ratio = 1 - semantic_unit_count / len(subtitles)
    terms = [
        unicodedata.normalize("NFKC", str(term)).strip()
        for term in (expected_terms or []) if str(term).strip()
    ]
    normalised_text = unicodedata.normalize("NFKC", text)
    entity_match_ratio = (
        sum(term in normalised_text for term in terms) / len(terms)
        if terms else 1.0
    )

    score = 0
    score += 20 if len(subtitles) >= 10 else 10 if len(subtitles) >= 3 else 0
    score += 35 if language_ratio >= 0.75 else 20 if language_ratio >= 0.45 else 0
    score += 25 if unique_ratio >= 0.45 else 12 if unique_ratio >= 0.25 else 0
    score += 20 if monotonic_ratio >= 0.98 else 10 if monotonic_ratio >= 0.9 else 0
    if micro_cue_ratio >= 0.05:
        score -= 15
    if fragment_ratio >= 0.2:
        score -= 15
    if rapid_short_cues:
        score -= 15
    if hallucination_loop_ratio >= 0.3:
        score -= 30
    if semantic_compression_ratio >= 0.5:
        score -= 10
    if terms and entity_match_ratio == 0:
        score -= 10
    score = max(0, score)

    reasons = []
    if len(subtitles) < 3:
        reasons.append("too_few_subtitles")
    if language_ratio < 0.45:
        reasons.append({"en": "not_english", "ja": "not_japanese", "ko": "not_korean"}[source_language])
    if unique_ratio < 0.25:
        reasons.append("high_repetition")
    if monotonic_ratio < 0.9:
        reasons.append("invalid_timeline")
    if micro_cue_ratio >= 0.05:
        reasons.append("rolling_fragments")
    if fragment_ratio >= 0.2:
        reasons.append("sentence_fragments")
    if rapid_short_cues:
        reasons.append("rapid_short_cues")
    if hallucination_loop_ratio >= 0.3:
        reasons.append("hallucination_loop")
    if semantic_compression_ratio >= 0.5:
        reasons.append("semantic_fragmentation")
    if terms and entity_match_ratio == 0:
        reasons.append("title_entities_missing")
    needs_timing_alignment = bool(long_cue_ratio >= 0.05)
    if needs_timing_alignment:
        reasons.append("timing_anomalies")
    usable = score >= 60 and len(subtitles) >= 3 and language_ratio >= 0.45
    needs_secondary = bool(
        usable and (
            micro_cue_ratio >= 0.05
            or fragment_ratio >= 0.2
            or rapid_short_cues
            or semantic_compression_ratio >= 0.5
            or (bool(terms) and entity_match_ratio == 0)
            or needs_timing_alignment
        )
    )
    return SourceQuality(
        usable,
        score,
        len(subtitles),
        round(language_ratio, 4),
        round(unique_ratio, 4),
        round(monotonic_ratio, 4),
        tuple(reasons),
        round(micro_cue_ratio, 4),
        round(fragment_ratio, 4),
        needs_secondary,
        round(long_cue_ratio, 4),
        needs_timing_alignment,
        source_language,
        round(short_cue_ratio, 4),
        round(hallucination_loop_ratio, 4),
        semantic_unit_count,
        round(semantic_compression_ratio, 4),
        round(entity_match_ratio, 4),
    )


def assess_english_srt(path: str | Path) -> SourceQuality:
    """Backward-compatible English wrapper for older jobs and integrations."""
    return assess_source_srt(path, source_language="en")
