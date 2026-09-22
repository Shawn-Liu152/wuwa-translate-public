"""Deterministic recovery of canonical English glossary entities from ASR text."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Mapping


@dataclass(frozen=True)
class EntityCandidate:
    surface: str
    canonical: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class NormalizedResult:
    text: str
    canonical: tuple[str, ...]
    confidence: str
    reason: str
    candidates: tuple[EntityCandidate, ...] = ()


def _default_data_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data",
    )


@lru_cache(maxsize=8)
def _load_en_glossary(data_dir: str) -> dict[str, str]:
    db_path = os.path.join(data_dir, "glossary.db")
    if not os.path.isfile(db_path):
        return {}
    uri = f"file:{os.path.abspath(db_path).replace(os.sep, '/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT source_term, target_term FROM glossary "
            "WHERE source_language='en' AND target_language='zh-CN'"
        ).fetchall()
    return {str(source): str(target) for source, target in rows if source and target}


@lru_cache(maxsize=8)
def _load_en_corrections(data_dir: str) -> dict[str, str]:
    path = os.path.join(data_dir, "asr_corrections_en.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        str(surface): str(canonical)
        for surface, canonical in payload.items()
        if surface and canonical
    }


def _entity_pattern(canonical: str) -> re.Pattern[str]:
    parts = re.findall(r"[A-Za-z0-9]+", canonical)
    body = r"[^A-Za-z0-9]+".join(re.escape(part) for part in parts)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _normalized_punctuation(value: str) -> str:
    return "".join(re.findall(r"[A-Za-z0-9]+", value)).lower()


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _replace_evidence_aliases(
    text: str, glossary: Mapping[str, str], corrections: Mapping[str, str],
) -> tuple[str, list[EntityCandidate]]:
    matches: list[EntityCandidate] = []
    canonical_by_lower = {term.lower(): term for term in glossary}
    aliases = {
        surface.lower(): canonical_by_lower[corrected.lower()]
        for surface, corrected in corrections.items()
        if corrected.lower() in canonical_by_lower
    }
    if not aliases:
        return text, matches
    body = "|".join(
        re.escape(surface) for surface in sorted(aliases, key=len, reverse=True)
    )
    pattern = re.compile(
        rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        canonical = aliases[match.group(0).lower()]
        matches.append(EntityCandidate(
            match.group(0), canonical, "high", "evidence-alias",
        ))
        return canonical

    return pattern.sub(replace, text), matches


def _replace_glossary_forms(
    text: str, glossary: Mapping[str, str],
) -> tuple[str, list[EntityCandidate]]:
    matches: list[EntityCandidate] = []
    for canonical in sorted(glossary, key=len, reverse=True):
        if not re.search(r"[A-Za-z0-9]", canonical):
            continue
        pattern = _entity_pattern(canonical)

        def replace(match: re.Match[str]) -> str:
            surface = match.group(0)
            if surface == canonical:
                reason = "exact"
            elif surface.lower() == canonical.lower():
                reason = "case-normalized"
            elif _normalized_punctuation(surface) == _normalized_punctuation(canonical):
                reason = "punctuation-normalized"
            else:
                return surface
            matches.append(EntityCandidate(surface, canonical, "high", reason))
            return canonical

        text = pattern.sub(replace, text)
    return text, matches


def _strip_attached_forms(
    text: str, glossary: Mapping[str, str],
) -> tuple[str, list[EntityCandidate]]:
    single_word_names = [
        term for term in glossary
        if len(term) >= 5 and term[:1].isupper() and term.isalpha()
    ]
    matches: list[EntityCandidate] = []

    def replace(match: re.Match[str]) -> str:
        surface = match.group(0)
        lowered = surface.lower()
        ranked: list[str] = []
        for canonical in single_word_names:
            official = canonical.lower()
            suffix = lowered[len(official):] if lowered.startswith(official) else ""
            prefix = lowered[:-len(official)] if lowered.endswith(official) else ""
            # missing_initial 恢复（如 ingran→Jingran）只对较长 surface
            # 生效：4 字符以内的普通英文词（over/no/ever/very 等）极易
            # 满足 "lowered == official[1:]"（如 over == rover[1:]），
            # 误判会把普通词污染成角色名（2026-08-10 "full over"→"full
            # Rover" 实证）。surface >= 5 才可能丢首字母。
            missing_initial = (
                len(lowered) >= 5 and lowered == official[1:]
            )
            attached = (
                1 <= len(suffix) <= 3 or 1 <= len(prefix) <= 2
            ) and (surface[:1].isupper() or any(char.isupper() for char in surface[1:]))
            if missing_initial or attached:
                ranked.append(canonical)
        if len(ranked) != 1:
            return surface
        canonical = ranked[0]
        matches.append(EntityCandidate(
            surface, canonical, "high", "unique-affix-stripping",
        ))
        return canonical

    return re.sub(r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])", replace, text), matches


def _similarity_candidates(text: str, glossary: Mapping[str, str]) -> list[EntityCandidate]:
    """相似度候选：支持 ASR 听写变体与官方形式的规范化串比较。

    2026-08-10 实证：`jin Gran` / `jin geon` / `jing ran` 是 Jingran 的
    常见 ASR 变体，`Froolova` 是 Phrolova 的变体。旧实现只枚举单个
    ≥5 字母 token 并与单词 canonical 比较，分词数变化的变体永远不参与
    比较 → 无法 canonicalize → LLM 自由音译出"金格兰"等假角色。

    新实现：对整个候选 surface（允许 1-3 个 token 的英文短语）与所有
    canonical 名做去空白/标点后的编辑距离 + 相似度比较，仅唯一且
    高置信（distance ≤ 2 或 ratio ≥ 0.82）的候选才返回。
    """
    candidates: list[EntityCandidate] = []
    canonical_names = [
        term for term in glossary
        if len(term) >= 5 and term[:1].isupper() and term.isalpha()
    ]
    if not canonical_names:
        return candidates
    # 单 token surface（原逻辑）：保持严格阈值——单 token 普通英文词
    # （met/and/here）极易与 canonical 短距离匹配，宽松会误伤。
    for surface in re.findall(r"(?<![A-Za-z])[A-Za-z]{5,}(?![A-Za-z])", text):
        if not surface[:1].isupper():
            continue
        lowered = surface.lower()
        ranked: list[tuple[float, int, str]] = []
        for canonical in canonical_names:
            official = canonical.lower()
            if lowered == official:
                continue
            distance = _edit_distance(lowered, official)
            ratio = SequenceMatcher(None, lowered, official).ratio()
            if distance <= 2 and ratio >= 0.82:
                ranked.append((ratio, -distance, canonical))
        ranked.sort(reverse=True)
        if not ranked:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            continue
        candidates.append(EntityCandidate(
            surface, ranked[0][2], "medium", "token-similarity-advisory",
        ))
    # 多 token surface（如 "jin Gran" / "Qing Xiao" / "jing ran"）：
    # 恰好 2 个 token（正则 `{1}` 只允许一个分隔符），去空白 + 标点后
    # 与 canonical 名比较，允许分词数变化。不要求首字母大写——ASR
    # 变体常小写（jin Gran 的首 token 就是小写），由编辑距离门槛过滤。
    # 排除 "canonical + 功能词" 组合（如 "Jingran is" → jingranis），
    # 这类是正常英语句子不是 ASR 变体。
    _FUNCTION_WORDS = {
        "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
        "of", "to", "for", "with", "in", "on", "at", "by", "it", "he",
        "she", "they", "this", "that", "met", "did", "has", "have", "had",
        "not", "his", "her", "my", "your", "our", "their", "then", "than",
    }
    for surface in re.findall(
        r"(?<![A-Za-z])[A-Za-z]+[ -][A-Za-z]+(?![A-Za-z])", text,
    ):
        tokens = surface.split()
        # Apostrophes split contractions into one-letter fragments (``'s``
        # / ``I``), which made ordinary grammar such as ``what's going`` and
        # ``I can`` look edit-close to Suoming/Jiyan.  Real multi-token ASR
        # names in this path (jin Gran, Qing Xiao) have no one-letter token.
        if any(len(re.sub(r"[^A-Za-z]", "", token)) < 2 for token in tokens):
            continue
        # 任一分词是功能词 → 这不是 ASR 人名变体
        if any(token.casefold() in _FUNCTION_WORDS for token in tokens):
            continue
        normalized = _normalized_punctuation(surface)
        if len(normalized) < 4:
            continue
        ranked = []
        for canonical in canonical_names:
            official = _normalized_punctuation(canonical)
            distance = _edit_distance(normalized, official)
            ratio = SequenceMatcher(None, normalized, official).ratio()
            # 多 token 相似度兜底（未收录变体，如 Froolova→Phrolova）：
            # 距离 ≤3 且长度差 ≤2（拦截 "met Qingxiao" 这类功能词尾巴
            # 造成的假接近），歧义由下方 top-2 差距检查拦截。
            if (
                distance <= 3
                and abs(len(normalized) - len(official)) <= 2
                and (ratio >= 0.72 or distance <= 2)
            ):
                ranked.append((ratio, -distance, canonical))
        ranked.sort(reverse=True)
        if not ranked:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            continue
        candidates.append(EntityCandidate(
            surface, ranked[0][2], "medium", "token-similarity-advisory",
        ))
    return candidates


def _apply_similarity_candidates(
    text: str, candidates: list[EntityCandidate],
) -> tuple[str, list[EntityCandidate]]:
    """应用唯一且高置信的相似度候选（自动 canonicalize）。

    2026-08-10 实证：`jin Gran`(→Jingran)、`Froolova`(→Phrolova) 这类
    ASR 变体若不自动恢复，LLM 会自由音译出"金格兰"等假角色。规则：
    - 候选必须唯一（同一 surface 只有一个 canonical 匹配）
    - surface 长度 ≥ 4，编辑距离 ≤ 2，相似度 ≥ 0.82
    - 被替换的 surface 保持首字母大写（canonical 形态）
    多候选歧义（两个 canonical 都接近）时保持原样，绝不强行替换。
    """
    applied: list[EntityCandidate] = []
    # 按 surface 分组，只处理唯一候选
    by_surface: dict[str, EntityCandidate] = {}
    for candidate in candidates:
        surface = candidate.surface
        lowered = surface.lower()
        if (
            lowered in by_surface
            and by_surface[lowered].canonical != candidate.canonical
        ):
            # 同一 surface 有两个不同 canonical → 歧义，撤销
            by_surface[lowered] = EntityCandidate(
                surface, "", "low", "ambiguous",
            )
        else:
            by_surface[lowered] = candidate

    result = text
    for surface, candidate in by_surface.items():
        if not candidate.canonical or candidate.confidence == "low":
            continue
        # 只应用高置信（唯一 + 距离足够近）
        if candidate.reason != "token-similarity-advisory":
            continue
        pattern = re.compile(
            rf"(?<![A-Za-z]){re.escape(candidate.surface)}(?![A-Za-z])",
            re.IGNORECASE,
        )
        if not pattern.search(result):
            continue
        result = pattern.sub(candidate.canonical, result)
        applied.append(EntityCandidate(
            candidate.surface, candidate.canonical, "high",
            "unique-similarity-canonicalized",
        ))
    return result, applied


def normalize_en_entities(
    text: str,
    glossary: Mapping[str, str] | None = None,
    data_dir: str | None = None,
) -> NormalizedResult:
    """Normalize high-confidence English entities and report weaker candidates."""
    original = text
    if not text:
        return NormalizedResult(text, (), "low", "unknown")
    resolved_data_dir = os.path.abspath(data_dir or _default_data_dir())
    terms = dict(glossary) if glossary is not None else _load_en_glossary(resolved_data_dir)
    if not terms:
        return NormalizedResult(text, (), "low", "unknown")

    text, alias_matches = _replace_evidence_aliases(
        text, terms, _load_en_corrections(resolved_data_dir),
    )
    text, glossary_matches = _replace_glossary_forms(text, terms)
    text, affix_matches = _strip_attached_forms(text, terms)
    high_matches = alias_matches + glossary_matches + affix_matches
    advisory = _similarity_candidates(text, terms)
    # 唯一高置信相似候选自动 canonicalize（多 token ASR 变体恢复）
    text, applied_similarity = _apply_similarity_candidates(text, advisory)
    high_matches = high_matches + applied_similarity
    # 应用后再扫一遍：canonical 已恢复的文本可能还有未命中的变体
    advisory = [c for c in advisory if c.surface not in {
        a.surface for a in applied_similarity
    }]
    canonical = tuple(dict.fromkeys(match.canonical for match in high_matches))
    if high_matches:
        reasons = tuple(dict.fromkeys(match.reason for match in high_matches))
        return NormalizedResult(
            text, canonical, "high", ", ".join(reasons), tuple(advisory),
        )
    if advisory:
        return NormalizedResult(
            original, (), "medium", "token-similarity-advisory", tuple(advisory),
        )
    return NormalizedResult(original, (), "low", "unknown")
