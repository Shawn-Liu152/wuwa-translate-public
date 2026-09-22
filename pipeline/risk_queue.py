"""Deterministic risk ranking for Round 2 subtitle review."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, asdict
from statistics import median
from typing import Callable, Dict, Iterable, List, Sequence

from pipeline.candidates import SourceCandidate, preferred_source_text
from pipeline.config import Config
from pipeline.entity_resolution import resolve_entity_candidates
from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms


@dataclass
class RiskItem:
    key: str
    subtitle_id: int
    start: str
    end: str
    score: int
    reasons: List[str]
    english: str
    capcut_en: str
    whisper_en: str
    translated: str
    source_language: str = "en"
    source_text: str = ""
    primary_evidence: str = ""
    secondary_evidence: str = ""
    severity: str = "advisory"
    review_required: bool = False
    term_hints: Dict[str, str] | None = None
    context_before: str = ""
    context_after: str = ""
    state: str = "pending"
    needs_audio_evidence: bool = False
    contextual_entities: List[str] | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        # ``english``/``capcut_en``/``whisper_en`` remain readable by old
        # queue consumers.  New consumers must use these language-labelled
        # fields; Japanese evidence is never treated as English evidence.
        payload["source_text"] = self.source_text or self.english
        payload["primary_evidence"] = self.primary_evidence or self.capcut_en
        payload["secondary_evidence"] = self.secondary_evidence or self.whisper_en
        return payload


def _numbers(text: str) -> set[str]:
    """Normalise numeric formatting without guessing translated number words."""
    return {
        re.sub(r"[.,]", "", value)
        for value in re.findall(r"\d+(?:[.,]\d+)?", text)
    }


def _contains_term(text: str, term: str, source_language: str = "en") -> bool:
    if not term.strip():
        return False
    if source_language == "ja":
        return unicodedata.normalize("NFKC", term) in unicodedata.normalize("NFKC", text)
    if re.search(r"[A-Za-z0-9]", term):
        escaped_term = re.escape(term).replace(r"\ ", r"\s+")
        return re.search(
            rf"(?<![A-Za-z0-9]){escaped_term}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        ) is not None
    return term in text


def _effective_source_evidence(*sources: str) -> tuple[str, ...]:
    """Return unique source-language evidence without merging transcripts."""
    evidence = []
    seen = set()
    for source in sources:
        value = str(source or "").strip()
        if value and value not in seen:
            evidence.append(value)
            seen.add(value)
    return tuple(evidence)


# Compatibility for callers that still import the historical helper.
_effective_english_evidence = _effective_source_evidence


_ENGLISH_RESIDUE_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "but", "by",
    "can", "do", "for", "from", "have", "he", "her", "his", "i", "if",
    "in", "is", "it", "me", "my", "no", "not", "of", "on", "or", "our",
    "she", "that", "the", "their", "this", "to", "was", "we", "what",
    "when", "where", "will", "with", "you", "your",
}

_ALLOWED_LATIN_TOKENS = {
    "AI", "AOE", "ATK", "BGM", "BOSS", "CD", "CPU", "DEF", "DMG", "DPS",
    "ER", "EXP", "FPS", "GPU", "HP", "NPC", "OST", "PC", "PVP", "SFX",
    "UI", "UID", "URL", "VPN",
}
_JAPANESE_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_KOREAN_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")


def _mask_literal_phrases(text: str, phrases: Iterable[str]) -> str:
    masked = text
    for phrase in sorted(set(phrases), key=len, reverse=True):
        if not phrase.strip():
            continue
        escaped = re.escape(phrase).replace(r"\ ", r"\s+")
        masked = re.sub(
            rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
            lambda match: " " * len(match.group(0)),
            masked,
            flags=re.IGNORECASE,
        )
    return masked


def _has_english_residue(
    text: str, *, allowed_phrases: Iterable[str] = (),
) -> bool:
    checked_text = _mask_literal_phrases(text, allowed_phrases)
    words = re.findall(r"\b[A-Za-z]{2,}\b", checked_text)
    for word in words:
        if word.upper() in _ALLOWED_LATIN_TOKENS:
            continue
        if word == word.lower() and word in _ENGLISH_RESIDUE_WORDS:
            return True
        # A title-cased/mixed-case token inside an otherwise Chinese subtitle is
        # usually an untranslated proper noun. Keep common all-caps game
        # abbreviations above, but require names such as Violet or Wangchuan to
        # be translated or reviewed.
        if any("\u3400" <= char <= "\u9fff" for char in checked_text):
            return True
    return False


def _has_japanese_kana_residue(text: str) -> bool:
    """Return whether a Chinese target still contains Japanese kana.

    Japanese and Simplified Chinese legitimately share Han characters. Kana is
    therefore the reliable, language-specific signal of untranslated Japanese
    surface text.
    """

    return bool(_JAPANESE_KANA_RE.search(text or ""))


def _has_korean_residue(text: str) -> bool:
    return bool(_KOREAN_HANGUL_RE.search(text or ""))


# ---- ASR 垃圾行检测 ------------------------------------------------------
#
# 这几类不是翻译缺陷，而是上游 ASR 把非语音内容或片尾署名当成了台词。
# music.py 已经剥掉音乐/掌声/音效类标注（data/music.txt + _CUE_LABEL），
# 这里只补它没有覆盖的部分，避免与既有清洗重复判定。

_CREDIT_LINE_RE = re.compile(
    r"(?i)\b(?:subtitles?|subs|subbing|captioning|captions|transcription|"
    r"transcribed|translation)\s+by\b"
    r"|\bcastingwords\b|\brev\.com\b|\bamara\.org\b"
    r"|\bopensubtitles\b|\bsubscene\b"
)
_NOISE_COMMAND_RE = re.compile(
    r"(?i)[\[\(\{]\s*(?:beep\w*|bleep\w*|inaudible\w*|unintelligible\w*|"
    r"muffled|crosstalk|static|noise|click\w*|alarm|siren|laughter|laughing|"
    r"chuckl\w+|sighs?|gasps?|cough\w+|whisper\w+|groan\w+|screaming|"
    r"speaking\s+(?:a\s+)?foreign\s+language|foreign\s+language)"
    # 真实 ASR 常带时间戳（[inaudible 00:12]），所以标签后允许一小段尾巴。
    r"\b[^\]\)\}]{0,24}[\]\)\}]"
    r"|\bignore\s+(?:the\s+)?noise\b"
)
_REPEAT_NORMALIZE_RE = re.compile(
    r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+"
)
# 复读单元的下限：口语里 "very good, very good" 这种短重复是正常表达，
# 只有足够长的整句回声才是 ASR 故障。
_REPEAT_UNIT_MIN_CHARACTERS = 12
_REPEAT_UNIT_MIN_WORDS = 3
# 实测踩坑：`wait wait wait wait wait wait wait wait` 与 `Woah woah woah...`
# 是真人反复喊话，词数够多但只有一个不同的词。真正的 ASR 回声是整句复述，
# 所以还要求重复单元里至少有这么多个不同的词。
_REPEAT_UNIT_MIN_DISTINCT_WORDS = 3
# 信息密度：相对阈值与绝对下限同时生效才命中。单看 chars_per_minute 无法
# 区分「长停顿里只说了一个词」和「ASR 整段漏识别」，前者在任何语言里都合法。
# 所以要求：时长够长 + 源文本几乎为空 + 密度明显低于本片典型水平。
_LOW_INFO_DENSITY_RATIO = 0.25
_LOW_INFO_DENSITY_MIN_DURATION_MS = 6000
_LOW_INFO_DENSITY_MAX_CHARACTERS = 3


def _has_credit_line(text: str) -> bool:
    """Detect end-credit attribution the ASR mistook for dialogue."""
    return bool(_CREDIT_LINE_RE.search(text or ""))


def _has_noise_command(text: str) -> bool:
    """Detect non-speech cues left behind after music/SFX stripping."""
    return bool(_NOISE_COMMAND_RE.search(text or ""))


def _has_repeated_clause(text: str) -> bool:
    """Detect one cue that is the same clause echoed back to back.

    归一化（转小写、去空白与标点）后前半必须与后半完全相等。对有空格分隔
    的语言再要求词级前后半也相等，并设最小长度与最小词数，避免把正常的
    口语重复当成 ASR 回声。
    """
    normalized = _REPEAT_NORMALIZE_RE.sub(" ", str(text or "").lower()).strip()
    if not normalized:
        return False
    compact = normalized.replace(" ", "")
    half = len(compact) // 2
    if (
        len(compact) % 2
        or half < _REPEAT_UNIT_MIN_CHARACTERS
        or compact[:half] != compact[half:]
    ):
        return False
    words = normalized.split()
    if len(words) < 2:
        # 中文/日文没有词分隔，字符级判定已经足够。
        return True
    word_half = len(words) // 2
    if len(words) % 2 or word_half < _REPEAT_UNIT_MIN_WORDS:
        return False
    unit = words[:word_half]
    if unit != words[word_half:]:
        return False
    return len(set(unit)) >= _REPEAT_UNIT_MIN_DISTINCT_WORDS


def _characters_per_minute(text: str, duration_ms: int) -> float:
    """Return visible characters per minute, or 0 when duration is unusable."""
    if duration_ms <= 0:
        return 0.0
    visible = len(re.sub(r"\s+", "", text or ""))
    return visible / (duration_ms / 60000.0)


AUTOMATIC_REVIEW_REASONS = {
    "empty_translation",
    "number_mismatch",
    "term_mismatch",
    "english_residue",
    "kana_residue",
    "hangul_residue",
    "negation_missing",
    "suspected_name",
    "incomplete_fragment",
    # Kept for manifests generated before the risk split.
    "semantic_marker_missing",
    # 2026-08-10 三语审计：这些 Source Truth 级风险此前不进自动 Reviewer，
    # 导致 EN unknown_entity(35)+text_conflict(25) 全部 defer、Round 2
    # eligible=0、English Reviewer 完全不执行（Froolova→"那个人" 漏网）。
    # 未知/幻觉专名、源冲突、源不可靠必须由 Reviewer 复核。
    "unknown_entity",
    "hallucinated_entity",
    "source_unresolved",
    "text_conflict",
    # ASR 垃圾行里的复读句与信息密度过低都可能误判（正常口语重复、稀疏
    # 对白），交 Reviewer 复核。credit_line / noise_command 是确定性垃圾，
    # 刻意不进本集合，只记分并进 TRIAGE_ADVISORY_REASONS，避免无谓放大
    # review_required_count。
    "repeated_clause",
    "low_info_density",
}

MANUAL_REVIEW_REASONS = {
    # Correctly translated names still deserve a human glance because ASR may
    # have selected the wrong person while producing a plausible translation.
    # This reason never blocks Round 2 or auto-replaces Round 1 by itself.
    "person_name_present",
    # A source-context brand may safely remain in Latin script for this task,
    # but only a human can confirm the brand identity and desired house style.
    "contextual_entity_preserved",
    "contextual_entity_unverified",
}

_SOURCE_FRAGMENT_RE = re.compile(
    r"\b(?:a|an|the|for|or|and|but|to|of|with|because|if|when|"
    r"which|who|whose|is|are|was|were|has|have|had|do|does|did|"
    r"my|your|our|their)\s*[,:;-]*$",
    re.IGNORECASE,
)
_TRANSLATION_FRAGMENT_RE = re.compile(
    r"(?:一个|一种|这个|那个|还挺|非常|有点|是个|为了|因为|如果|"
    r"关于|作为|或者|以及|并且|但是|不过|而且|或|和|跟|但|"
    r"对|在|从|向|把|被|给|比|很|第|的)$"
)


def _is_incomplete_fragment(english: str, translated: str,
                            source_language: str = "en") -> bool:
    source = re.sub(r"(?:>>+|\[[^\]]+\])", " ", english).strip()
    if source_language == "ja":
        target = translated.strip()
        return bool(
            source and target
            and re.search(r"(?:\u306e|\u3068|\u306b|\u3066|\u304c|\u3092|\u306f|\u3082|\u304b\u3089|\u306a\u306e)\s*$", source)
            and re.search(r"(?:的|和|与|在|对|把|让|如果|因为|为了)$", target)
        )
    target = translated.strip().rstrip("，,、；;：:")
    return bool(
        source
        and target
        and _SOURCE_FRAGMENT_RE.search(source)
        and _TRANSLATION_FRAGMENT_RE.search(target)
    )


def _has_suspected_forgotten_name(english: str) -> bool:
    """Catch likely ASR-corrupted names only when the dialogue says it forgot one."""
    if ">>" not in english:
        return False
    lowered = english.casefold()
    forgot_context = re.search(
        r"\b(?:don'?t|do not|can'?t|cannot)\b.{0,28}"
        r"\b(?:remember|recall)\b|\bforgot(?:ten)?\b",
        lowered,
    )
    if not forgot_context:
        return False
    after_marker = english.split(">>", 1)[1].strip()
    return re.match(r"[A-Z][A-Za-z]{2,}(?:\s+something)?\b", after_marker) is not None


def _semantic_marker_issues(english: str, translated: str,
                            source_language: str = "en") -> set[str]:
    if source_language == "ja":
        has_negative = bool(re.search(
            r"(?:\u306a\u3044|\u306a\u304f|\u306c|\u307e\u305b\u3093|\u7121\u3057|\u4e0d|\u975e)",
            english,
        ))
        has_condition = bool(re.search(
            r"(?:\u3082\u3057|\u306a\u3089|\u3070|\u305f\u3089|\u306a\u3051\u308c\u3070|\u9664\u3044\u3066|\u5834\u5408)",
            english,
        ))
        negative_markers = ("不", "没", "无", "非", "别", "未", "莫", "休想")
        condition_markers = ("如果", "要是", "除非", "只要", "一旦", "的话", "是否", "能不能", "会不会")
        issues = set()
        if has_negative and not any(marker in translated for marker in negative_markers):
            issues.add("negation_missing")
        if has_condition and not any(marker in translated for marker in condition_markers):
            issues.add("condition_marker_missing")
        return issues
    if source_language == "ko":
        has_negative = bool(re.search(
            r"(?:^|\s)(?:안|못)(?:\s|$)|지\s*않|는\s*건\s*아니|절대.{0,8}않|없",
            english,
        ))
        has_condition = bool(re.search(
            r"(?:면|다면|거든|니까|지만|는데)(?:\s|[,，.]|$)", english,
        ))
        issues = set()
        if has_negative and not any(marker in translated for marker in (
            "不", "没", "无", "非", "别", "未", "绝不", "不会", "不能",
        )):
            issues.add("negation_missing")
        if has_condition and not any(marker in translated for marker in (
            "如果", "要是", "只要", "因为", "所以", "但是", "不过", "的话",
        )):
            issues.add("condition_marker_missing")
        return issues
    lowered = english.casefold()
    negative_source = re.sub(
        r"\b(?:no wonder|no doubt|not only)\b", "", lowered
    )
    condition_source = re.sub(r"\bas if\b", "", lowered)
    has_negative = re.search(
        r"\b(no|not|never|without|neither|nor)\b", negative_source
    )
    has_condition = re.search(
        r"\b(if|unless|whether)\b", condition_source
    )
    negative_markers = (
        "不", "没", "未", "无", "别", "并非", "绝非", "从不", "绝不",
        "免", "缺", "少", "零", "休想", "不曾",
    )
    condition_markers = (
        "如果", "若", "要是", "除非", "只要", "是否", "的话",
        "能不能", "会不会", "到底", "取决于",
    )
    issues = set()
    if has_negative and not any(marker in translated for marker in negative_markers):
        issues.add("negation_missing")
    has_chinese_choice = re.search(
        r"([\u3400-\u9fff])不\1", translated
    )
    if (
        has_condition
        and not any(marker in translated for marker in condition_markers)
        and not has_chinese_choice
    ):
        issues.add("condition_marker_missing")
    return issues


def _semantic_marker_missing(english: str, translated: str,
                             source_language: str = "en") -> bool:
    """Backward-compatible boolean used by the Round 2 validation gate."""
    return bool(_semantic_marker_issues(english, translated, source_language))


def build_risk_queue(
    candidates: Iterable[SourceCandidate],
    translated_subs: Iterable[Subtitle],
    glossary: Dict[str, str] | None = None,
    person_glossary: Dict[str, str] | None = None,
    is_pure_music: Callable[[str], bool] | None = None,
    clean_music: Callable[[str], str] | None = None,
    source_language: str | None = None,
) -> List[RiskItem]:
    """Create a stable list of subtitles deserving a second LLM pass."""
    glossary = glossary or {}
    person_glossary = person_glossary or {}
    candidate_list = list(candidates)
    by_time = {(sub.start, sub.end): sub for sub in translated_subs}
    items: List[RiskItem] = []

    # 信息密度是相对信号，得先看全片典型水平；口径与主循环一致（同一份
    # preferred_source_text，同样过 clean_music），否则中位数会被标注拉偏。
    densities: List[float] = []
    for candidate in candidate_list:
        density_source = preferred_source_text(candidate)
        if clean_music is not None:
            density_source = clean_music(density_source)
        rate = _characters_per_minute(
            density_source,
            timestamp_to_ms(candidate.end) - timestamp_to_ms(candidate.start),
        )
        if rate > 0:
            densities.append(rate)
    low_density_floor = (
        median(densities) * _LOW_INFO_DENSITY_RATIO if densities else 0.0
    )

    for candidate_index, candidate in enumerate(candidate_list):
        translated = by_time.get((candidate.start, candidate.end))
        text = translated.text if translated else ""
        candidate_language = source_language or candidate.source_language or "en"
        english = preferred_source_text(candidate)
        capcut_en = candidate.capcut_en
        whisper_en = candidate.whisper_en
        if clean_music is not None:
            text = clean_music(text)
            english = clean_music(english)
            capcut_en = clean_music(capcut_en)
            whisper_en = clean_music(whisper_en)
        # Empty/punctuation-only source rows and pure music/SFX cues carry no
        # translatable meaning. They must not consume Round 2 requests.
        if (
            not english.strip()
            or not any(character.isalnum() for character in english)
            or (is_pure_music is not None and is_pure_music(english))
        ):
            continue
        reasons: List[str] = []
        score = 0

        contextual_entities: list[str] = []
        source_for_term_matching = english
        if candidate_language == "en":
            contextual_entities = list(dict.fromkeys(
                item["surface"]
                for item in resolve_entity_candidates(
                    english,
                    {**glossary, **person_glossary},
                    source_language="en",
                )
                if item.get("classification") == "contextual_entity"
            ))
            source_for_term_matching = _mask_literal_phrases(
                english, contextual_entities,
            )

        source_flags = set(candidate.flags or [])
        if "text_conflict" in source_flags:
            reasons.append("text_conflict")
            score += 3
        if not text.strip():
            reasons.append("empty_translation")
            score += 10
        # 以下三项判定的是 ASR 源文本：垃圾来自上游识别，不是译文缺陷。
        if _has_credit_line(english):
            reasons.append("credit_line")
            score += 3
        if _has_noise_command(english):
            reasons.append("noise_command")
            score += 2
        if _has_repeated_clause(english):
            reasons.append("repeated_clause")
            score += 4
        english_numbers = _numbers(english)
        translated_numbers = _numbers(text)
        # Only compare machine-readable numbers on both sides. Translating
        # "100" as "一百" is valid and should not be an automatic risk.
        if english_numbers and translated_numbers and english_numbers != translated_numbers:
            reasons.append("number_mismatch")
            score += 5
        preserved_contextual_entities = [
            surface for surface in contextual_entities
            if _contains_term(text, surface, "en")
        ]
        if preserved_contextual_entities:
            reasons.append("contextual_entity_preserved")
            score += 2
        elif contextual_entities:
            reasons.append("contextual_entity_unverified")
            score += 2
        if candidate_language == "en" and _has_english_residue(
            text, allowed_phrases=preserved_contextual_entities,
        ):
            reasons.append("english_residue")
            score += 3
        if candidate_language == "ja" and _has_japanese_kana_residue(text):
            reasons.append("kana_residue")
            score += 4
        if candidate_language == "ko" and _has_korean_residue(text):
            reasons.append("hangul_residue")
            score += 4
        term_hints = {
            term: chinese
            for term, chinese in glossary.items()
            if _contains_term(source_for_term_matching, term, candidate_language)
        }
        person_hints = {
            term: chinese
            for term, chinese in person_glossary.items()
            if any(
                _contains_term(evidence, term, candidate_language)
                for evidence in _effective_source_evidence(
                    english, capcut_en, whisper_en
                )
            )
        }
        matched_person_targets = set(person_hints.values())
        for term, chinese in person_glossary.items():
            target = str(chinese).strip()
            if (
                len(re.sub(r"\s+", "", target)) >= 2
                and target in text
                and target not in matched_person_targets
            ):
                person_hints[term] = target
                matched_person_targets.add(target)
        if person_hints:
            reasons.append("person_name_present")
            score += 3
        if any(chinese not in text for chinese in term_hints.values()):
            reasons.append("term_mismatch")
            score += 5
        if not term_hints and _has_suspected_forgotten_name(english):
            reasons.append("suspected_name")
            score += 5
        duration = timestamp_to_ms(candidate.end) - timestamp_to_ms(candidate.start)
        visible_characters = len(re.sub(r"\s+", "", text))
        if (
            duration > 0
            and visible_characters >= 18
            and visible_characters / (duration / 1000) > 14
        ):
            reasons.append("reading_speed")
            score += 2
        # reading_speed 看的是译文显示速度；这里看的是源文本信息量。
        source_characters = len(re.sub(r"\s+", "", english))
        if (
            duration >= _LOW_INFO_DENSITY_MIN_DURATION_MS
            and 0 < source_characters <= _LOW_INFO_DENSITY_MAX_CHARACTERS
            and _characters_per_minute(english, duration) < low_density_floor
        ):
            reasons.append("low_info_density")
            score += 2
        semantic_issues = _semantic_marker_issues(
            english, text, candidate_language,
        )
        if "negation_missing" in semantic_issues:
            reasons.append("negation_missing")
            score += 6
        if "condition_marker_missing" in semantic_issues:
            reasons.append("condition_marker_missing")
            score += 2
        if _is_incomplete_fragment(english, text, candidate_language):
            reasons.append("incomplete_fragment")
            score += 7

        if reasons:
            reason_set = set(reasons)
            review_required = bool(
                reason_set.intersection(
                    AUTOMATIC_REVIEW_REASONS | MANUAL_REVIEW_REASONS
                )
            )
            severity = (
                "high" if review_required and score >= 6
                else "medium" if review_required
                else "advisory"
            )
            needs_audio = bool(
                "text_conflict" in source_flags
                or ("empty_translation" in reasons and source_flags)
                or (
                    "incomplete_fragment" in reasons
                    and (
                        ("whisper_missing" in source_flags
                         or "secondary_missing" in source_flags)
                        or "text_conflict" in source_flags
                    )
                )
            )
            before = ""
            after = ""
            if candidate_index:
                before = preferred_source_text(candidate_list[candidate_index - 1])
            if candidate_index + 1 < len(candidate_list):
                after = preferred_source_text(candidate_list[candidate_index + 1])
            items.append(RiskItem(
                key=candidate.key, subtitle_id=translated.id if translated else 0,
                start=candidate.start, end=candidate.end, score=score,
                reasons=sorted(set(reasons)), english=english,
                capcut_en=capcut_en, whisper_en=whisper_en,
                translated=text, severity=severity,
                source_language=candidate_language, source_text=english,
                primary_evidence=capcut_en, secondary_evidence=whisper_en,
                review_required=review_required,
                term_hints={**term_hints, **person_hints},
                context_before=before,
                context_after=after,
                needs_audio_evidence=needs_audio,
                contextual_entities=contextual_entities,
            ))

    return sorted(items, key=lambda item: (-item.score, item.start, item.key))


# ---- O-1 triage: derive auto-resolved entries out of the human queue --------
#
# This only labels entries.  ``review_required`` is never rewritten, so the
# generated/round2/review stores stay byte-identical and ``_delivery_status``
# keeps returning ``review_required``.  Production still needs an explicit
# human sign-off.

# Advisory-only signals never describe a translation defect on their own.
# credit_line / noise_command 必须列在这里：否则 triage_risk_item 会把它们
# 当成 extra 信号，让本来可以 advisory_only 自动分流的条目卡在人工队列。
TRIAGE_ADVISORY_REASONS = frozenset({
    "reading_speed", "condition_marker_missing",
    "credit_line", "noise_command",
})
# Two transcripts disagree.  For English we trust the CapCut reference that
# the pipeline already translated from.
TRIAGE_CONFLICT_REASONS = frozenset({"text_conflict", "source_unresolved"})
TRIAGE_UNKNOWN_ENTITY = "unknown_entity"
# O-1 激进分流（scope=names）：round2 高置信 keep 时，这些"实体/人名"类原因
# 可被信任放行。hallucinated_entity 与内容类不在其中——它们只在 scope=all 时
# 才随 keep&hi 一并放行（用户已知错误风险，靠 5% 抽检兜底）。
TRIAGE_TRUST_NAME_REASONS = frozenset({
    "unknown_entity", "person_name_present",
    "contextual_entity_preserved", "contextual_entity_unverified",
})

TRIAGE_SPOT_CHECK_SEED = "subtitle-pipeline-o1-triage-v1"
TRIAGE_SPOT_CHECK_PERCENT = 5

_EXTERNAL_ENTITIES_CACHE: Dict[str, tuple] = {}


def load_external_entities(path: str | None = None) -> dict:
    """Load the O-1 triage whitelist (external entities + mis-flagged words).

    Kept separate from ``data/glossary.db`` on purpose: the glossary holds
    task terminology and must stay untouched, while this file only decides
    whether a risk entry can leave the human queue.
    """
    if path is None:
        path = Config.EXTERNAL_ENTITIES_FILE
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {"entities": frozenset(), "false_positive_words": frozenset()}
    cached = _EXTERNAL_ENTITIES_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError):
        return {"entities": frozenset(), "false_positive_words": frozenset()}
    entities = frozenset(
        _normalize_surface(entry)
        for entry in (raw.get("entities") or [])
        if _normalize_surface(entry)
    )
    words = frozenset(
        _normalize_surface(entry)
        for entry in (raw.get("false_positive_words") or [])
        if _normalize_surface(entry)
    )
    payload = {"entities": entities, "false_positive_words": words}
    _EXTERNAL_ENTITIES_CACHE[path] = (stamp, payload)
    return payload


def _normalize_surface(text: object) -> str:
    """Lower-case and collapse punctuation so ASR variants match one entry."""
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    return " ".join(re.split(r"[^0-9a-z\u4e00-\u9fff]+", raw)).strip()


def _item_value(item: object, field: str, default: object = "") -> object:
    if isinstance(item, dict):
        value = item.get(field, default)
    else:
        value = getattr(item, field, default)
    return default if value is None else value


def split_risk_reasons(reasons: Sequence[object] | None) -> tuple[set, dict]:
    """Split ``kind``/``kind:payload`` reason strings into kinds and surfaces."""
    kinds: set = set()
    surfaces: dict = {}
    for raw in reasons or ():
        text = str(raw or "").strip()
        if not text:
            continue
        kind, _, payload = text.partition(":")
        kind = kind.strip()
        if not kind:
            continue
        kinds.add(kind)
        if payload:
            found = [p.strip() for p in payload.split(",") if p.strip()]
            if found:
                surfaces[kind] = found
    return kinds, surfaces


def is_whitelisted_surface(surface: object, whitelist: dict | None = None) -> bool:
    """True when a flagged surface is a known brand or a mis-flagged word."""
    if whitelist is None:
        whitelist = load_external_entities()
    entities = whitelist.get("entities") or frozenset()
    words = whitelist.get("false_positive_words") or frozenset()
    norm = _normalize_surface(surface)
    if not norm:
        return False
    if norm in words or norm in entities:
        return True
    tokens = norm.split()
    if tokens and all(token in words for token in tokens):
        return True
    # Multi-word brands may appear inside a longer ASR span
    # (``From Bandai Namco Entertainment`` -> ``bandai namco``).
    for entity in entities:
        if " " in entity and re.search(
            r"(?<![0-9a-z])" + re.escape(entity) + r"(?![0-9a-z])", norm,
        ):
            return True
    return False


def is_spot_check(key: object, percent: int = TRIAGE_SPOT_CHECK_PERCENT) -> bool:
    """Deterministic sampling: the same key always yields the same verdict."""
    if percent <= 0:
        return False
    digest = hashlib.sha256(
        f"{TRIAGE_SPOT_CHECK_SEED}:{key}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16) % 100 < percent


def triage_risk_item(item: object, whitelist: dict | None = None) -> tuple[bool, str]:
    """Decide whether a generated risk entry may leave the human queue.

    Returns ``(auto_resolvable, reason)``.  Only signals that are fully
    explained by a whitelist / a resolved source conflict / an advisory-only
    hint are auto-resolved; anything carrying a content-level signal stays.
    """
    if whitelist is None:
        whitelist = load_external_entities()
    if not bool(_item_value(item, "review_required", False)):
        return False, ""
    kinds, surfaces = split_risk_reasons(_item_value(item, "reasons", []) or [])
    if not kinds:
        return False, ""
    extra = kinds - TRIAGE_ADVISORY_REASONS
    if not extra:
        return True, "advisory_only"

    # O-1 激进分流：信任 round2 的高置信 keep（scope 由 Config 控制）。
    # 只影响派生 state，绝不改 review_required；5% 抽检与审计字段照旧生效。
    scope = Config.RISK_TRUST_KEEP_SCOPE
    if scope != "off":
        decision = str(_item_value(item, "round2_decision", "")).strip().lower()
        try:
            confidence = float(_item_value(item, "round2_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            decision == "keep"
            and confidence >= Config.RISK_TRUST_KEEP_CONFIDENCE
        ):
            if scope == "all":
                return True, "trusted_keep"
            if extra <= TRIAGE_TRUST_NAME_REASONS | TRIAGE_CONFLICT_REASONS:
                return True, "trusted_keep_name"

    hard = extra - TRIAGE_CONFLICT_REASONS - {TRIAGE_UNKNOWN_ENTITY}
    if hard:
        return False, ""

    if extra <= TRIAGE_CONFLICT_REASONS:
        # Strategy 2: both transcripts disagreed, but the translation was
        # already produced from the CapCut reference.
        if str(_item_value(item, "source_language", "")).lower() != "en":
            return False, ""
        primary = str(_item_value(item, "primary_evidence", "")).strip()
        if not primary:
            primary = str(_item_value(item, "capcut_en", "")).strip()
        if not primary:
            return False, ""
        return True, "source_conflict_en"

    if extra == {TRIAGE_UNKNOWN_ENTITY}:
        # Strategy 1: every flagged surface is a known brand or a common word
        # the entity guard should never have flagged.
        flagged = surfaces.get(TRIAGE_UNKNOWN_ENTITY) or []
        if not flagged:
            return False, ""
        if all(is_whitelisted_surface(s, whitelist) for s in flagged):
            return True, "whitelisted_entity"
        return False, ""

    return False, ""
