"""Build a lossless, language-aware union from two subtitle sources."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Iterable, List

from pipeline.languages import normalize_language_pair
from pipeline.parser.srt_parser import (
    Subtitle, coverage_ratio, overlap_ms, subtitle_key, timestamp_to_ms,
)


@dataclass
class SourceCandidate:
    key: str
    start: str
    end: str
    primary_evidence: str = ""
    secondary_evidence: str = ""
    primary_source: str = "primary"
    secondary_source: str = "secondary"
    source_language: str = "en"
    primary_coverage: float = 0.0
    secondary_coverage: float = 0.0
    flags: List[str] | None = None
    # M-04 (2026-08-11): secondary 证据自身的时间轴。双源仲裁选中
    # secondary 为 canonical 时，display timing 应来自 secondary
    # （Whisper 时间更准），而不是永远硬选 primary。
    secondary_start: str = ""
    secondary_end: str = ""

    # Compatibility accessors for persisted English risk payloads and scripts.
    @property
    def capcut_en(self) -> str:
        return self.primary_evidence

    @property
    def whisper_en(self) -> str:
        return self.secondary_evidence

    @property
    def source(self) -> str:
        return self.primary_source

    @property
    def capcut_coverage(self) -> float:
        return self.primary_coverage

    @property
    def whisper_coverage(self) -> float:
        return self.secondary_coverage

    def to_dict(self) -> dict:
        payload = asdict(self)
        if self.source_language == "en":
            payload.update({
                "capcut_en": self.primary_evidence,
                "whisper_en": self.secondary_evidence,
                "source": self.primary_source,
                "capcut_coverage": self.primary_coverage,
                "whisper_coverage": self.secondary_coverage,
            })
        return payload


class EnglishCandidate(SourceCandidate):
    """Legacy constructor retained for existing English callers."""

    def __init__(
        self, key: str, start: str, end: str, capcut_en: str = "",
        whisper_en: str = "", source: str = "", capcut_coverage: float = 0.0,
        whisper_coverage: float = 0.0, flags: List[str] | None = None,
    ):
        super().__init__(
            key=key, start=start, end=end,
            primary_evidence=capcut_en, secondary_evidence=whisper_en,
            primary_source=source or "capcut", secondary_source="whisper",
            source_language="en", primary_coverage=capcut_coverage,
            secondary_coverage=whisper_coverage, flags=flags,
        )

    @classmethod
    def from_source(cls, candidate: SourceCandidate) -> "EnglishCandidate":
        return cls(
            candidate.key, candidate.start, candidate.end,
            candidate.primary_evidence, candidate.secondary_evidence,
            candidate.primary_source, candidate.primary_coverage,
            candidate.secondary_coverage, candidate.flags,
        )


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_english(text: str) -> bool:
    return len(_LATIN_RE.findall(text or "")) >= 2


def preferred_source_text(candidate: SourceCandidate) -> str:
    """Choose the declared-language primary evidence without cross-language fallback."""
    primary = candidate.primary_evidence.strip()
    secondary = candidate.secondary_evidence.strip()
    if candidate.source_language in {"ja", "ko"}:
        return primary or secondary
    if primary and not _CJK_RE.search(primary) and _has_english(primary):
        return primary
    if secondary and _has_english(secondary):
        return secondary
    if primary and _has_english(primary):
        return primary
    return ""


def preferred_english(candidate: SourceCandidate) -> str:
    """Backward-compatible English selector."""
    return preferred_source_text(candidate)


def _best_match(target: Subtitle, candidates: Iterable[Subtitle]) -> tuple[Subtitle | None, float]:
    best = None
    best_ratio = 0.0
    for candidate in candidates:
        ratio = coverage_ratio(target, candidate)
        if ratio > best_ratio or (
            ratio == best_ratio and best
            and overlap_ms(target, candidate) > overlap_ms(target, best)
        ):
            best, best_ratio = candidate, ratio
    return best, best_ratio


def _best_matches(targets: Iterable[Subtitle], candidates: Iterable[Subtitle]) -> dict[str, tuple[Subtitle | None, float]]:
    ordered_targets = sorted(targets, key=lambda item: (timestamp_to_ms(item.start), timestamp_to_ms(item.end)))
    ordered_candidates = sorted(candidates, key=lambda item: (timestamp_to_ms(item.start), timestamp_to_ms(item.end)))
    active: list[Subtitle] = []
    cursor = 0
    matches: dict[str, tuple[Subtitle | None, float]] = {}
    for target in ordered_targets:
        start_ms = timestamp_to_ms(target.start)
        end_ms = timestamp_to_ms(target.end)
        active = [item for item in active if timestamp_to_ms(item.end) > start_ms]
        while cursor < len(ordered_candidates) and timestamp_to_ms(ordered_candidates[cursor].start) < end_ms:
            candidate = ordered_candidates[cursor]
            if timestamp_to_ms(candidate.end) > start_ms:
                active.append(candidate)
            cursor += 1
        matches[subtitle_key(target)] = _best_match(target, active)
    return matches


def _normalise_text(text: str, source_language: str = "en") -> str:
    if source_language in {"ja", "ko"}:
        return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text))
    return " ".join(re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")))


def _texts_conflict(primary: str, secondary: str, source_language: str) -> bool:
    left = _normalise_text(primary, source_language)
    right = _normalise_text(secondary, source_language)
    if not left or not right or left == right:
        return False
    if left in right or right in left:
        shorter, longer = sorted((len(left), len(right)))
        if shorter / max(longer, 1) >= 0.72:
            return False
    return SequenceMatcher(None, left, right).ratio() < 0.78


def _interval_distance_ms(first: Subtitle, second: Subtitle) -> int:
    first_start, first_end = timestamp_to_ms(first.start), timestamp_to_ms(first.end)
    second_start, second_end = timestamp_to_ms(second.start), timestamp_to_ms(second.end)
    if first_end < second_start:
        return second_start - first_end
    if second_end < first_start:
        return first_start - second_end
    return 0


def _combined_coverage_ratio(target: Subtitle, references: Iterable[Subtitle]) -> float:
    """Return target coverage by the union of all reference intervals."""
    target_start = timestamp_to_ms(target.start)
    target_end = timestamp_to_ms(target.end)
    duration = target_end - target_start
    if duration <= 0:
        return 0.0
    intervals = sorted(
        (
            max(target_start, timestamp_to_ms(item.start)),
            min(target_end, timestamp_to_ms(item.end)),
        )
        for item in references
        if overlap_ms(target, item) > 0
    )
    covered = 0
    cursor_start = cursor_end = None
    for start, end in intervals:
        if cursor_start is None:
            cursor_start, cursor_end = start, end
        elif start <= cursor_end:
            cursor_end = max(cursor_end, end)
        else:
            covered += cursor_end - cursor_start
            cursor_start, cursor_end = start, end
    if cursor_start is not None:
        covered += cursor_end - cursor_start
    return covered / duration


def build_source_union(
    primary_subs: List[Subtitle], secondary_subs: List[Subtitle],
    min_coverage: float = 0.5, source_language: str = "en",
    primary_source: str = "primary", secondary_source: str = "secondary",
) -> List[SourceCandidate]:
    """Preserve each source separately; conflicting texts are never concatenated."""
    source_language = normalize_language_pair({
        "source_language": source_language,
    })["source_language"]
    union: List[SourceCandidate] = []
    matched_secondary_ids: set[str] = set()
    secondary_matches = _best_matches(primary_subs, secondary_subs)
    primary_matches = _best_matches(secondary_subs, primary_subs)
    primary_by_text: dict[str, list[Subtitle]] = {}
    for primary in primary_subs:
        normalised = _normalise_text(primary.text, source_language)
        if normalised:
            primary_by_text.setdefault(normalised, []).append(primary)

    for primary in primary_subs:
        secondary, ratio = secondary_matches.get(subtitle_key(primary), (None, 0.0))
        flags: List[str] = []
        secondary_text = ""
        # 2026-08-11 Holdout 修复：排除已被更早 primary 消费的 secondary。
        # Whisper 粗分段长 cue 覆盖多个 primary 短 cue 时，若不加过滤，
        # combined 分支会把同一文本广播给所有重叠 primary → union 出现
        # 多条相同文本 → 翻译重复（holdout-l9NCconvSNA 实证）。
        overlapping_secondary = [
            item for item in secondary_subs
            if subtitle_key(item) not in matched_secondary_ids
            and overlap_ms(primary, item) > 0
        ]
        combined_ratio = _combined_coverage_ratio(primary, overlapping_secondary)
        if source_language in {"ja", "ko"} and combined_ratio >= min_coverage:
            joiner = "" if source_language == "ja" else " "
            ordered = sorted(
                overlapping_secondary,
                key=lambda item: (timestamp_to_ms(item.start), timestamp_to_ms(item.end)),
            )
            secondary_text = joiner.join(item.text.strip() for item in ordered if item.text.strip())
            ratio = combined_ratio
            matched_secondary_ids.update(subtitle_key(item) for item in ordered)
            if _texts_conflict(primary.text, secondary_text, source_language):
                flags.append("text_conflict")
        elif (
            secondary is not None
            and subtitle_key(secondary) not in matched_secondary_ids
            and ratio >= min_coverage
        ):
            secondary_text = secondary.text
            matched_secondary_ids.add(subtitle_key(secondary))
            if _texts_conflict(primary.text, secondary.text, source_language):
                flags.append("text_conflict")
        else:
            flags.append(
                "whisper_missing" if source_language == "en"
                else "secondary_missing"
            )
        if source_language == "en" and not _has_english(primary.text) and _has_english(secondary_text):
            flags.append("secondary_english_preferred")
        # M-04: secondary 证据时间轴（可能为空——未匹配到 secondary 时
        # 保持空，display timing 继续用 primary）
        if secondary_text:
            if source_language in {"ja", "ko"} and ordered:
                sec_start = ordered[0].start
                sec_end = ordered[-1].end
            elif secondary is not None:
                sec_start, sec_end = secondary.start, secondary.end
            else:
                sec_start = sec_end = ""
        else:
            sec_start = sec_end = ""
        union.append(SourceCandidate(
            key=subtitle_key(primary), start=primary.start, end=primary.end,
            primary_evidence=primary.text, secondary_evidence=secondary_text,
            primary_source=primary_source, secondary_source=secondary_source,
            source_language=source_language, primary_coverage=1.0,
            secondary_coverage=ratio, flags=flags,
            secondary_start=sec_start, secondary_end=sec_end,
        ))

    for secondary in secondary_subs:
        if subtitle_key(secondary) in matched_secondary_ids:
            continue
        # 注意：不再用 primary_matches ratio >= min_coverage 直接丢弃——
        # 双视角阈值不对称（primary 视角 combined_ratio < 阈值未吸收、
        # secondary 视角 ratio >= 阈值）会把未吸收的转写静默丢弃，
        # 导致同一句主播话的完整转写丢失。保留它，交给 _merge_nested_candidates
        # 在时间窗完全包含时并入外层候选。
        if (
            source_language in {"ja", "ko"}
            and _combined_coverage_ratio(secondary, primary_subs) >= min_coverage
        ):
            # 阈值不对称兜底：secondary 完全被某个 primary 覆盖（secondary 视角
            # ratio >= 阈值），但 primary 视角 combined_ratio 可能 < 阈值而未吸收。
            # 不能丢弃——把文本并入外层候选的证据，保留完整转写；
            # 未完全覆盖时（如跨两个 primary 的日语证据）也不新增独立候选。
            for primary in primary_subs:
                if coverage_ratio(secondary, primary) >= min_coverage:
                    for cand in union:
                        if cand.key == subtitle_key(primary):
                            _merge_evidence(cand, secondary.text)
                            break
                    break
            continue
        normalised = _normalise_text(secondary.text, source_language)
        if normalised and any(
            _interval_distance_ms(secondary, primary_item) <= 2_000
            for primary_item in primary_by_text.get(normalised, [])
        ):
            continue
        union.append(SourceCandidate(
            key=subtitle_key(secondary), start=secondary.start, end=secondary.end,
            primary_evidence="", secondary_evidence=secondary.text,
            primary_source=primary_source, secondary_source=secondary_source,
            source_language=source_language, primary_coverage=ratio,
            secondary_coverage=1.0,
            flags=[
                "capcut_missing" if source_language == "en"
                else "primary_missing"
            ],
            secondary_start=secondary.start, secondary_end=secondary.end,
        ))

    union.sort(key=lambda item: (item.start, item.end, item.key))
    return _merge_nested_candidates(union)


def _merge_evidence(candidate: SourceCandidate, text: str) -> None:
    """把一条转写文本并入候选的证据，更完整的文本提升为主证据。"""
    text = (text or "").strip()
    if not text:
        return
    primary = (candidate.primary_evidence or "").strip()
    secondary = (candidate.secondary_evidence or "").strip()
    if not primary:
        candidate.primary_evidence = text
        return
    if len(text) > len(primary):
        candidate.primary_evidence = text
        text = primary
    if text and text not in secondary:
        candidate.secondary_evidence = (secondary + " " + text).strip() if secondary else text


def _merge_nested_candidates(candidates: List[SourceCandidate]) -> List[SourceCandidate]:
    """合并时间窗完全包含在其他候选内的双源候选。

    真实素材（主播 reaction 合集）暴露：Whisper 转写的时间窗完全包含在
    YouTube 候选内（同句不同转写，覆盖率不足 50% 未被吸收），保留为独立
    语义单元后回填互相打架，产生 1ms 废显示 cue。
    合并规则：子候选的证据并入外层候选；更完整的转写提升为主文本（翻译
    用更长更全的转写），外层时间窗保持不变。
    """
    merged: List[SourceCandidate] = []
    for candidate in candidates:
        child_start = timestamp_to_ms(candidate.start)
        child_end = timestamp_to_ms(candidate.end)
        parent = None
        for existing in merged:
            p_start = timestamp_to_ms(existing.start)
            p_end = timestamp_to_ms(existing.end)
            if child_start >= p_start and child_end <= p_end:
                parent = existing
                break
        if parent is None:
            merged.append(candidate)
            continue
        child_text = (
            candidate.primary_evidence or candidate.secondary_evidence
        ).strip()
        if not child_text:
            continue
        parent_text = (parent.primary_evidence or "").strip()
        parent_secondary = (parent.secondary_evidence or "").strip()
        # 更完整的转写提升为主文本，短的降级为证据
        if len(child_text) > len(parent_text):
            parent.primary_evidence = child_text
            child_text = parent_text
        if child_text and child_text not in parent_secondary:
            parent.secondary_evidence = (
                parent_secondary + " " + child_text
            ).strip()
        # 子候选自己的辅助证据也并入
        child_secondary = (candidate.secondary_evidence or "").strip()
        if (
            child_secondary
            and child_secondary not in parent.secondary_evidence
            and child_secondary != parent.primary_evidence
        ):
            parent.secondary_evidence = (
                parent.secondary_evidence + " " + child_secondary
            ).strip()
    return merged


def build_english_union(
    capcut_subs: List[Subtitle], whisper_subs: List[Subtitle],
    min_coverage: float = 0.5,
) -> List[EnglishCandidate]:
    """Backward-compatible English union wrapper."""
    return [EnglishCandidate.from_source(candidate) for candidate in build_source_union(
        capcut_subs, whisper_subs, min_coverage=min_coverage,
        source_language="en", primary_source="capcut", secondary_source="whisper",
    )]


def union_to_subtitles(candidates: Iterable[SourceCandidate]) -> List[Subtitle]:
    """Create LLM input from the declared-language source without renumbering IDs."""
    subtitles = []
    for index, candidate in enumerate(candidates, start=1):
        text = preferred_source_text(candidate)
        if text:
            subtitles.append(Subtitle(index, candidate.start, candidate.end, text))
    return subtitles
