"""Long-video subtitle workflow with durable, independently resumable stages.

The first English SRT owns the timeline. The second English SRT is supporting
evidence. Round 1 translates the complete union; Round 2 only revisits
deterministically selected risks and never overwrites the Round 1 artifact.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from pipeline.candidates import (
    build_source_union,
    preferred_source_text,
    union_to_subtitles,
)
from pipeline.config import Config
from pipeline.languages import normalize_language_pair
from pipeline.local_transcribe import build_windows
from pipeline.parser.srt_parser import Subtitle, parse_srt, save_srt, timestamp_to_ms
from pipeline.preprocess.cue_classifier import CueClass, classify_cue
from pipeline.preprocess.music import load_music_detector, strip_music_annotations
from pipeline.pipeline_manifest import (
    PipelineCancelled,
    create_manifest,
    file_sha256,
    load_manifest,
    partial_batch_results,
    save_manifest,
    set_batch,
    successful_batch_results,
    summarize_manifest_metrics,
    validate_resume,
    raise_if_cancelled,
)
from pipeline.risk_queue import (
    AUTOMATIC_REVIEW_REASONS,
    _has_english_residue,
    _is_incomplete_fragment,
    _numbers,
    _semantic_marker_issues,
    build_risk_queue,
)
from pipeline.risk_queue_store import (
    ensure_review_state,
    load_generated_risks,
    load_round2_results,
    merge_risk_views,
    save_generated_risks,
    save_round2_results,
)
from pipeline.safe_errors import public_error_fields
from pipeline.translate.llm import (
    LLMAPIError,
    LLMTransientError,
    create_translator,
    thinking_policy_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
log = logging.getLogger("long_video")

# 日文假名 → 常见中文音译字（2026-08-11 Phase 7 幻觉名 gate 用）。
# 只覆盖清音，用于判定"候选音译 run 是否与源文专名词对应"，
# 不完全精确但足够区分真音译（蕾姆塔洛斯←レムタロス）与编造名
# （泽尔卡恩←エイメス 无共享音译字）。
_KANA_TO_ZH: dict[str, set[str]] = {
    "ア": {"阿", "亚"}, "イ": {"伊", "依"}, "ウ": {"乌", "宇"}, "エ": {"埃", "艾"},
    "オ": {"奥", "欧"}, "カ": {"卡", "加"}, "キ": {"基", "奇", "琪"}, "ク": {"库", "古"},
    "ケ": {"凯", "开"}, "コ": {"科", "寇", "柯"}, "サ": {"萨", "莎", "沙"}, "シ": {"希", "西", "茜"},
    "ス": {"斯", "苏"}, "セ": {"塞", "赛"}, "ソ": {"索", "所"}, "タ": {"塔", "他", "泰"},
    "チ": {"奇", "琪", "蒂", "姬"}, "ツ": {"茨", "兹", "楚"}, "テ": {"特", "忒"}, "ト": {"托", "特", "图"},
    "ナ": {"娜", "那"}, "ニ": {"妮", "尼"}, "ヌ": {"努", "奴"}, "ネ": {"妮", "涅", "内"},
    "ノ": {"诺", "野"}, "ハ": {"哈", "巴", "华"}, "ヒ": {"希", "比", "绯"}, "フ": {"芙", "夫", "弗"},
    "ヘ": {"赫", "海"}, "ホ": {"霍", "郝", "禾"}, "マ": {"玛", "马", "麻"}, "ミ": {"米", "弥"},
    "ム": {"姆", "穆"}, "メ": {"梅", "玫", "妹"}, "モ": {"莫", "默", "茉"}, "ヤ": {"亚", "雅"},
    "ユ": {"尤", "优", "由"}, "ヨ": {"约", "阳", "曜"}, "ラ": {"拉", "喇", "兰"},
    "リ": {"莉", "丽", "里", "利"}, "ル": {"露", "鲁", "卢"}, "レ": {"蕾", "莱", "雷", "勒"},
    "ロ": {"洛", "罗", "萝"}, "ワ": {"瓦", "华"}, "ヲ": {"沃", "奥"}, "ン": {"恩", "姆", "呣"},
    "ガ": {"加", "伽", "卡"}, "ギ": {"基", "吉", "琪"}, "グ": {"古", "谷", "库"},
    "ゲ": {"盖", "格", "凯"}, "ゴ": {"戈", "哥", "科"}, "ザ": {"扎", "泽", "萨"},
    "ジ": {"吉", "基", "姬", "智"}, "ズ": {"兹", "祖"}, "ゼ": {"泽", "则"}, "ゾ": {"佐", "索"},
    "ダ": {"达", "大", "妲"}, "ヂ": {"奇", "琪"}, "ヅ": {"兹", "祖"}, "デ": {"迪", "德", "黛"},
    "ド": {"多", "朵", "德"}, "バ": {"巴", "芭", "帕"}, "ビ": {"比", "碧", "毗"},
    "ブ": {"布", "步", "部"}, "ベ": {"贝", "蓓"}, "ボ": {"博", "波", "伯"},
    "パ": {"帕", "琶"}, "ピ": {"匹", "皮", "碧"}, "プ": {"普", "璞"}, "ペ": {"佩", "佩"},
    "ポ": {"波", "珀", "坡"}, "ャ": {"亚", "雅"}, "ュ": {"尤", "优"}, "ョ": {"约", "曜"},
    "ッ": {"", "特", "尔"},
}
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

ROUND_TWO_BATCH_SIZE = 8
ROUND_TWO_SCHEMA = "subtitle-decisions-v5"
ROUND_TWO_CONFIDENCE_THRESHOLD = 0.85
# 高置信阈值：≥0.95 的修正视为"有把握的证据驱动修复"，跳过相似度
# 硬拒绝与启发式误报检查（JA gl8vjOL1xWI 实证：0.97-0.98 的正确修复
# 曾被 excessive_rewrite 误拒）。
ROUND_TWO_CONFIDENCE_THRESHOLD_HIGH = 0.95


def _load_glossary(game: str, data_dir: str = None,
                   source_language: str = "en") -> dict[str, str]:
    from pipeline.preprocess.glossary import load_glossary_db

    database = load_glossary_db(data_dir)
    return {
        item["english"]: item["chinese"]
        for item in database.list_all(game=game, source_language=source_language)
    }


def _load_review_glossary(
    game: str,
    data_dir: str = None,
    source_language: str = "en",
) -> dict[str, str]:
    """Return only proper/fixed terms suitable for deterministic QA."""
    from pipeline.preprocess.glossary import load_glossary_db

    strict_categories = {
        "character", "story_character", "character_alias",
        "location", "faction", "company", "enemy", "lore", "echo",
        "element",
    }
    strict_system_terms = {
        "Echo", "Lament", "Resonator", "Tacet Mark",
        "Waveworn Phenomenon",
    }
    result = {}
    for item in load_glossary_db(data_dir).list_all(
        game=game, source_language=source_language,
    ):
        if (
            item.get("category") in strict_categories
            or (
                item.get("category") == "system"
                and item.get("english") in strict_system_terms
            )
        ):
            result[item["english"]] = item["chinese"]
    return result


def _load_person_glossary(
    game: str,
    data_dir: str = None,
    source_language: str = "en",
) -> dict[str, str]:
    """Return names that always need explicit human confirmation."""
    from pipeline.preprocess.glossary import load_glossary_db

    person_categories = {
        "character", "story_character", "character_alias",
        "external_character",
    }
    return {
        item["english"]: item["chinese"]
        for item in load_glossary_db(data_dir).list_all(
            game=game, source_language=source_language,
        )
        if item.get("category") in person_categories
    }


def _all_source_risk_evidence(candidates) -> str:
    """Join every usable transcript variant for source-matched QA hints."""
    evidence = []
    seen = set()
    for candidate in candidates:
        for source in (
            preferred_source_text(candidate),
            candidate.primary_evidence,
            candidate.secondary_evidence,
        ):
            value = str(source or "").strip()
            if value and value not in seen:
                evidence.append(value)
                seen.add(value)
    return "\n".join(evidence)


_all_english_risk_evidence = _all_source_risk_evidence


_ALIAS_REGEX_MARKERS = re.compile(r"[\[\](){}.*+?^$|/]")


def _source_matches_alias(source_text: str, alias: str,
                          source_language: str = "en") -> bool:
    if _ALIAS_REGEX_MARKERS.search(alias):
        try:
            return re.search(alias, source_text, re.IGNORECASE) is not None
        except re.error:
            return False
    if source_language in {"ja", "ko"}:
        return alias in source_text
    return _term_is_present(source_text, alias)


def _source_matched_person_aliases(
    person_glossary: dict[str, str],
    source_text: str,
    data_dir: str = None,
    source_language: str = "en",
) -> dict[str, str]:
    """Expose only reliable, source-seen aliases for mandatory name review."""
    alias_path = Path(data_dir or (PROJECT_ROOT / "data")) / "alias.json"
    if not source_text.strip() or not alias_path.is_file():
        return {}
    try:
        raw_aliases = json.loads(alias_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw_aliases, dict):
        return {}

    known_people = {
        str(english).strip().casefold(): chinese
        for english, chinese in person_glossary.items()
        if str(english).strip() and str(chinese).strip()
    }
    matched = {}
    for alias, canonical_name in raw_aliases.items():
        alias_text = str(alias).strip()
        chinese = known_people.get(str(canonical_name).strip().casefold())
        if alias_text and chinese and _source_matches_alias(
            source_text, alias_text, source_language,
        ):
            matched[alias_text] = chinese
    return matched


def _add_fuzzy_name_hints(
    glossary: dict[str, str],
    source_text: str,
    game: str,
    data_dir: str = None,
    source_language: str = "en",
) -> dict[str, str]:
    """Add only source-matched aliases; never inject the full alias universe."""
    from pipeline.preprocess.glossary import load_glossary_db

    if not source_text.strip():
        return glossary
    database = load_glossary_db(data_dir)
    extracted = database.extract_from_text(
        source_text, game=game, source_language=source_language,
    )
    official_terms = {
        str(item["english"]).casefold()
        for item in database.list_all(game=game, source_language=source_language)
    }
    strict_terms = {term.casefold() for term in glossary}
    matched = {
        term: chinese
        for term, chinese in extracted.items()
        if (
            term.casefold() in strict_terms
            or term.casefold() not in official_terms
        )
    }
    # The shipped ASR typo list is English-only.  Do not apply its regexes to
    # Japanese text, where accidental Latin matching would mix evidence.
    if source_language != "en":
        return {**glossary, **matched}
    tricky_path = Path(data_dir or (PROJECT_ROOT / "data")) / "tricky_terms.json"
    if tricky_path.is_file():
        try:
            tricky_terms = json.loads(tricky_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tricky_terms = []
        for item in tricky_terms:
            pattern = str(item.get("pattern", "")).strip()
            if item.get("hard", True) is False:
                for existing in list(matched):
                    if existing.casefold() == pattern.casefold():
                        matched.pop(existing, None)
                continue
            chinese = str(item.get("correct", "")).strip()
            escaped_pattern = re.escape(pattern).replace(r"\ ", r"\s+")
            if (
                pattern
                and chinese
                and re.search(
                    rf"(?<![A-Za-z0-9]){escaped_pattern}(?![A-Za-z0-9])",
                    source_text,
                    re.IGNORECASE,
                )
            ):
                matched[pattern] = chinese
    return {**glossary, **matched}


def _stage_paths(output: str, source_language: str = "en") -> dict[str, str]:
    """Return stable artifact names without changing the public final filename."""
    target = Path(output)
    if target.name.endswith(".zh.srt"):
        base_name = target.name[:-len(".zh.srt")]
        round1 = target.with_name(f"{base_name}.round1.zh.srt")
        round2 = target.with_name(f"{base_name}.round2.zh.srt")
    else:
        round1 = target.with_name(f"{target.stem}.round1{target.suffix}")
        round2 = target.with_name(f"{target.stem}.round2{target.suffix}")
    legacy_prefix = str(target.with_suffix(""))
    return {
        "final": str(target),
        "round1": str(round1),
        "round1_translation": str(round1) + ".translated-only.srt",
        "round2": str(round2),
        "round1_manifest": str(round1) + ".manifest.json",
        "round2_manifest": str(round2) + ".manifest.json",
        "union": legacy_prefix + f".{source_language}.union.final.srt",
        "translation_input": legacy_prefix + f".{source_language}.translate.srt",
        "display_map": legacy_prefix + ".display-map.json",
        "risk_generated": legacy_prefix + ".risk.generated.json",
        "round2_results": legacy_prefix + ".round2.results.json",
        "review_state": legacy_prefix + ".review-state.json",
        "metrics": legacy_prefix + ".pipeline.metrics.json",
        "evidence_dir": legacy_prefix + ".evidence",
        "reconstruction_trace": legacy_prefix + ".reconstruction.json",
    }


def _publish_final(
    source: str, destination: str, display_map: str | None = None,
    *, allow_source_residue: bool = False,
) -> None:
    """Atomically publish a cleaned final while preserving stage files."""
    from pipeline.preprocess.dedupe import clean_export_subtitles

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    subtitles = clean_export_subtitles(parse_srt(source))
    from pipeline.postprocess.punctuation import normalize_punctuation
    for subtitle in subtitles:
        subtitle.text = normalize_punctuation(subtitle.text)
    if display_map and Path(display_map).is_file():
        from pipeline.display_cues import redistribute_subtitles
        mapping = json.loads(Path(display_map).read_text(encoding="utf-8"))
        subtitles = redistribute_subtitles(subtitles, mapping)
        # Display splitting may place an internal sentence stop at a cue end.
        # The subtitle contract omits final full stops, but keeps commas and
        # expressive punctuation that carry reading rhythm.
        for subtitle in subtitles:
            subtitle.text = re.sub(r"[.。]+$", "", subtitle.text.rstrip())
    from pipeline.display_cues import enforce_minimum_duration, enforce_reading_rhythm
    subtitles = enforce_reading_rhythm(subtitles)
    subtitles = enforce_minimum_duration(subtitles)
    from pipeline.postprocess.validate import validate_display_cues
    validate_display_cues(
        subtitles, allow_source_residue=allow_source_residue,
    )
    save_srt(str(temporary), subtitles)
    os.replace(temporary, destination_path)


def _write_union(
    primary_path: str, secondary_path: str, union_path: str,
    source_language: str = "en", display_map_path: str | None = None,
):
    from pipeline.preprocess.asr_normalize import (
        load_asr_corrections, normalize_asr_text,
    )
    from pipeline.preprocess.dedupe import (
        coalesce_micro_cues, coalesce_semantic_cues, repair_rolling_overlaps,
    )

    # ASR 听错归一化：YouTube 自动字幕/Whisper 的人名术语变体先修正
    # 为标准形式（如 데미야/데니야 → 데니아），再进入滚动重叠修复与
    # 语义合并，保证术语匹配与翻译输入一致（data/asr_corrections.json）。
    raw_primary = repair_rolling_overlaps(
        parse_srt(primary_path), source_language=source_language,
    )
    raw_secondary = repair_rolling_overlaps(
        parse_srt(secondary_path), source_language=source_language,
    )
    corrections = load_asr_corrections(source_language=source_language)
    for subtitle in (*raw_primary, *raw_secondary):
        subtitle.text = normalize_asr_text(subtitle.text, corrections)
    # YouTube 滚动窗口文本去重（Step 1: 合并相似窗口取更长版本;
    # Step 2: 修剪开头重复）——防止 coalesce_semantic_cues 拼接时
    # 产生重复文本，传染到最终翻译。
    from pipeline.preprocess.dedupe import dedupe_youtube_subs
    raw_primary = dedupe_youtube_subs(
        raw_primary, source_language=source_language,
    )
    primary = coalesce_semantic_cues(
        coalesce_micro_cues(raw_primary), source_language=source_language,
    )
    secondary = coalesce_semantic_cues(
        coalesce_micro_cues(raw_secondary), source_language=source_language,
    )
    candidates = build_source_union(
        primary, secondary, source_language=source_language,
        primary_source="primary", secondary_source="secondary",
    )
    Path(union_path).parent.mkdir(parents=True, exist_ok=True)
    if source_language == "en":
        # The legacy English selector intentionally rejects pure CJK.  Mixed
        # subtitle jobs need those rows preserved until cue classification.
        union_subtitles = []
        for index, candidate in enumerate(candidates, start=1):
            text = (
                preferred_source_text(candidate)
                or candidate.primary_evidence.strip()
                or candidate.secondary_evidence.strip()
            )
            if text:
                union_subtitles.append(Subtitle(
                    index, candidate.start, candidate.end, text,
                ))
    else:
        union_subtitles = union_to_subtitles(candidates)
    if source_language == "en":
        from pipeline.preprocess.en_entity_normalize import normalize_en_entities
        for subtitle in union_subtitles:
            subtitle.text = normalize_en_entities(subtitle.text).text
    # ja/ko：逐句证据仲裁 → canonical 覆盖 union 文本（en 保持旧链路）
    arbitrated = {}
    if source_language in {"ja", "ko"}:
        from pipeline.preprocess.source_arbitration import arbitrate_candidates
        from dataclasses import asdict
        from pipeline.config import Config
        arbitrated = arbitrate_candidates(
            candidates, source_language=source_language,
            data_dir=getattr(Config, "DATA_DIR", None),
        )
        if arbitrated:
            by_time = {
                (unit.start_ms, unit.end_ms): unit
                for unit in arbitrated.values()
            }
            for sub in union_subtitles:
                unit = by_time.get((
                    timestamp_to_ms(sub.start), timestamp_to_ms(sub.end),
                ))
                if unit and unit.canonical_source_text:
                    sub.text = unit.canonical_source_text
            try:
                _atomic_json(
                    union_path + ".arbitration.json",
                    {key: asdict(unit) for key, unit in arbitrated.items()},
                )
            except (OSError, ValueError, TypeError):
                pass
    save_srt(union_path, union_subtitles)
    if display_map_path:
        mapping: dict[str, list[dict[str, str]]] = {}
        output_index = 0
        # M-04 (2026-08-11): display timing 不再永远硬选 primary。
        # ja/ko 仲裁选中 secondary 为 canonical 时（source_decision 为
        # secondary/single_secondary），时间窗与 spans 取自 secondary
        # 证据自身时间轴（Whisper 时间通常更准）；否则仍用 primary。
        for candidate in candidates:
            selected_text = preferred_source_text(candidate)
            if source_language == "en":
                selected_text = (
                    selected_text
                    or candidate.primary_evidence.strip()
                    or candidate.secondary_evidence.strip()
                )
            if not selected_text:
                continue
            output_index += 1
            use_secondary_timing = False
            if source_language in {"ja", "ko"} and candidate.secondary_start:
                unit = arbitrated.get(candidate.key)
                if unit is not None and unit.source_decision in (
                    "secondary", "single_secondary",
                ):
                    use_secondary_timing = True
            if use_secondary_timing:
                raw_source = raw_secondary
                start_ms = timestamp_to_ms(candidate.secondary_start)
                end_ms = timestamp_to_ms(candidate.secondary_end)
            else:
                raw_source = raw_primary if candidate.primary_evidence else raw_secondary
                start_ms = timestamp_to_ms(candidate.start)
                end_ms = timestamp_to_ms(candidate.end)
            spans = [
                {"start": item.start, "end": item.end}
                for item in raw_source
                if timestamp_to_ms(item.end) > start_ms
                and timestamp_to_ms(item.start) < end_ms
            ]
            mapping[str(output_index)] = spans or [{
                "start": _format_timestamp(start_ms),
                "end": _format_timestamp(end_ms),
            }]
        _atomic_json(display_map_path, mapping)
    return candidates, len(raw_primary), len(raw_secondary)


def _prepare_en_translation_input(
    union_path: str,
    translation_path: str,
    *,
    data_dir: str | None = None,
) -> dict[int, CueClass]:
    """Write only English cues to the Round 1 input and return all classes."""
    detector = load_music_detector(data_dir)
    subtitles = parse_srt(union_path)
    classes = {
        subtitle.id: classify_cue(
            subtitle.text,
            source_language="en",
            music_detector=detector,
        )
        for subtitle in subtitles
    }
    save_srt(
        translation_path,
        [
            subtitle for subtitle in subtitles
            if classes[subtitle.id] in {
                CueClass.ENGLISH_REACTION, CueClass.MIXED,
            }
        ],
    )
    return classes


def _merge_en_classified_round1(
    union_path: str,
    translated_path: str | None,
    output_path: str,
    classes: dict[int, CueClass],
) -> None:
    """Restore bypassed cues around the translated English Round 1 output."""
    translated = (
        {item.id: item.text for item in parse_srt(translated_path)}
        if translated_path and Path(translated_path).is_file() else {}
    )
    merged = []
    for subtitle in parse_srt(union_path):
        cue_class = classes[subtitle.id]
        if cue_class in {
            CueClass.ENGLISH_REACTION, CueClass.MIXED,
        }:
            text = translated.get(subtitle.id, "")
        elif cue_class == CueClass.MUSIC_SFX:
            text = ""
        else:
            text = subtitle.text
        merged.append(type(subtitle)(
            subtitle.id, subtitle.start, subtitle.end, text,
        ))
    save_srt(output_path, merged)


def _merge_cue_classification_risks(
    risks,
    candidates,
    round1_subtitles,
    classes: dict[int, CueClass],
):
    """Replace automatic risks for bypassed cues with manual-only risks."""
    from pipeline.risk_queue import RiskItem

    manual_ids = {
        subtitle_id for subtitle_id, cue_class in classes.items()
        if cue_class in {CueClass.MIXED, CueClass.UNKNOWN}
    }
    bypass_ids = {
        subtitle_id for subtitle_id, cue_class in classes.items()
        if cue_class not in {
            CueClass.ENGLISH_REACTION, CueClass.MIXED,
        }
    }
    merged = [item for item in risks if int(item.subtitle_id) not in bypass_ids]
    candidate_by_time = {
        (candidate.start, candidate.end): candidate for candidate in candidates
    }
    for subtitle in round1_subtitles:
        if subtitle.id not in manual_ids:
            continue
        candidate = candidate_by_time.get((subtitle.start, subtitle.end))
        source_text = (
            (preferred_source_text(candidate) or subtitle.text)
            if candidate else subtitle.text
        )
        cue_class = classes[subtitle.id]
        primary = candidate.primary_evidence if candidate else source_text
        secondary = candidate.secondary_evidence if candidate else ""
        merged.append(RiskItem(
            key=f"cue-classification:{subtitle.id}|{subtitle.start}|{subtitle.end}",
            subtitle_id=subtitle.id,
            start=subtitle.start,
            end=subtitle.end,
            score=8,
            reasons=[f"cue_classification_{cue_class.value}"],
            english=source_text,
            capcut_en=primary,
            whisper_en=secondary,
            translated=subtitle.text,
            source_language="en",
            source_text=source_text,
            primary_evidence=primary,
            secondary_evidence=secondary,
            severity="high",
            review_required=True,
        ))
    return sorted(merged, key=lambda item: (-item.score, item.start, item.key))


def _term_is_present(text: str, term: str, source_language: str = "en") -> bool:
    if source_language in {"ja", "ko"}:
        return term in text
    if re.search(r"[A-Za-z0-9]", term):
        escaped_term = re.escape(term).replace(r"\ ", r"\s+")
        return re.search(
            rf"(?<![A-Za-z0-9]){escaped_term}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        ) is not None
    return term in text


def _relevant_glossary(items: list[dict], glossary: dict[str, str]) -> dict[str, str]:
    source = "\n".join(
        f"{item.get('source_text', item.get('english', ''))}\n"
        f"{item.get('primary_evidence', item.get('capcut_en', ''))}\n"
        f"{item.get('secondary_evidence', item.get('whisper_en', ''))}"
        for item in items
    )
    source_language = str(
        (items[0].get("source_language") if items else "en") or "en"
    )
    relevant = {
        english: chinese
        for english, chinese in glossary.items()
        if _term_is_present(source, english, source_language)
    }
    for item in items:
        for english, chinese in (item.get("term_hints") or {}).items():
            if english and chinese:
                relevant[str(english)] = str(chinese)
    return relevant


def _local_evidence_text(item: dict) -> str:
    evidence_text = str(item.get("local_evidence_text", "")).strip()
    if evidence_text or not item.get("local_evidence"):
        return evidence_text
    try:
        return strip_music_annotations(" ".join(
            subtitle.text
            for subtitle in parse_srt(str(item["local_evidence"]))
        ))
    except (OSError, ValueError):
        return ""


def _round_two_prompt(batch: list[dict], glossary: dict[str, str],
                      source_language: str = "en") -> str:
    payload = []
    for item in batch:
        payload.append({
            "id": int(item["subtitle_id"]),
            "source_language": source_language,
            "source_text": strip_music_annotations(str(
                item.get("source_text", item.get("english", ""))
            )),
            "english": strip_music_annotations(str(item.get("english", ""))),
            "primary_evidence": strip_music_annotations(str(
                item.get("primary_evidence", item.get("capcut_en", ""))
            )),
            "secondary_evidence": strip_music_annotations(str(
                item.get("secondary_evidence", item.get("whisper_en", ""))
            )),
            "local_audio_evidence": _local_evidence_text(item),
            "round1_chinese": strip_music_annotations(str(item.get("translated", ""))),
            "risk_reasons": item.get("reasons", []),
            "required_name_terms": item.get("term_hints") or {},
            "context_before": strip_music_annotations(
                str(item.get("context_before", ""))
            ),
            "context_after": strip_music_annotations(
                str(item.get("context_after", ""))
            ),
        })
    if source_language in {"ja", "ko"}:
        language_label = "日语" if source_language == "ja" else "韩语"
        language_rules = (
            "保留否定、条件、数字、专名、敬称和说话语气；不得把日语证据当作英语处理。"
            if source_language == "ja" else
            "保留否定、条件、数字、专名、敬语等级和说话语气；不得把韩语证据当作英语处理。"
        )
        return json.dumps({
            "task": f"复核{language_label}到简体中文的 Reaction 字幕；只在明确{language_label}证据表明 Round 1 有误时做最小修正。",
            "rules": [
                f"source_text 是仲裁后的标准源文本（canonical），是翻译的唯一依据；primary_evidence 和 secondary_evidence 是两份独立{language_label} ASR 证据，仅供参考（advisory），不可替代 source_text。",
                f"禁止用 primary_evidence 或 secondary_evidence 覆盖 source_text 来重新选择翻译源；你的职责是检查中文是否忠于 source_text，不是重新仲裁双源。",
                "只有当 source_text 明显残缺或语义不通时，才可参考 primary/secondary 做最小补全，且必须在 reason 中说明 source_text 的具体问题。",
                language_rules,
                "已知人名必须逐条人工确认。人名正确时返回 keep，不得仅凭人名触发 replace。",
                "逐项检查 Round 1：漏译、多译、主语/宾语错误、否定翻反、疑问/反问错误、数字错误、人名/术语错误、虚构人名、语气不自然、生硬直译；有明确证据才 replace。",
                "risk_reasons 含 kana_residue 或 hangul_residue 时，keep 无效，必须 replace；text 必须是无日文假名、无韩文字符的简体中文。专名也应使用已知中文名或保守中文音译，不得保留源语言文字。",
                "不得引入 glossary 中不存在的新中文人名/地名（疑似幻觉实体）；无法判断时保守 keep 并降低 confidence。",
                "context_before 和 context_after 仅供理解，不得翻译到当前字幕。",
                "只允许 keep 或 replace；每个输入 id 必须返回一个结果，且不增删、合并或拆分 id。",
                f"Round 1 准确时 keep 且原样返回；replace 只能修正有明确{language_label}证据的错误。",
                "只返回 JSON 对象，不要 Markdown。",
            ],
            "output_schema": {
                "results": [{
                    "id": "integer matching input id",
                    "decision": "keep or replace",
                    "text": "non-empty Simplified Chinese subtitle",
                    "confidence": "number from 0 to 1",
                    "reason": "short Chinese justification citing Japanese evidence",
                }]
            },
            "glossary": glossary,
            "items": payload,
        }, ensure_ascii=False, separators=(",", ":"))
    if source_language == "en":
        instructions = {
            "task": "复核英语到简体中文的 Reaction 字幕；只在明确 canonical English 证据表明 Round 1 有误时做最小修正。",
            "rules": [
                "source_text 是 canonical English（source of truth），是翻译和复核的唯一依据；primary_evidence 和 secondary_evidence 是 raw ASR，只提供 advisory diagnostic context，不得替代 source_text。",
                "禁止根据 primary_evidence、secondary_evidence 或其他上下文重新选择 source；Reviewer 不负责重新仲裁来源，也不得把 Round 1 当作草稿重新翻译整句。",
                "逐项检查 Round 1：omission、addition、negation、question、number、proper noun、terminology、subject-object、reaction intensity、literal-Chinese stiffness、hallucinated entity。",
                "以 Round 1 为锚，默认 KEEP（协议值为默认 keep）；含义、语气、数字和专名准确时，decision=keep 且 text 必须原样返回 Round 1。",
                "REPLACE 必须在 reason 中指出具体 canonical source span、对应的 Round 1 error、错误原因，并仅给出 minimal corrected translation；不能只写“翻译不自然”“优化表达”等空泛理由。",
                "replace 只能修正有明确 canonical English 和结构化 risk evidence 支持的错误，禁止为了润色改写正确内容。",
                "risk_reasons 中的 unknown_entity 或 hallucinated_entity 是 T7-C Entity Resolver 的风险证据；遇到这类证据必须更保守，禁止引入 glossary 或 required_name_terms 外的新中文人名、地名或专名，也不得自行音译。",
                "required_name_terms 只包含当前条已确认的人名或专名；普通动作短语不得按人名解释，例如 pulling for 表示抽角色，绝不是卜灵。",
                "risk_reasons 含 incomplete_fragment 时，可结合相邻上下文补足当前条被截断的语法成分，但不得重复翻译相邻条内容。",
                "context_before 和 context_after 仅供理解，不得翻译到当前 id，也不得增删、合并、拆分或改变 id。",
                "decision 只允许 keep 或 replace；results 必须逐一覆盖所有输入 items。",
                f"本次输入 {len(payload)} 条，results 也必须恰好 {len(payload)} 条。",
                "只返回 JSON 对象，不要 Markdown。",
            ],
            "output_schema": {
                "results": [{
                    "id": "integer, must exactly match an input id",
                    "decision": "keep or replace",
                    "text": "non-empty Simplified Chinese subtitle",
                    "confidence": "number from 0 to 1",
                    "reason": "short Chinese justification citing canonical English evidence",
                }]
            },
            "glossary": glossary,
            "items": payload,
        }
        return json.dumps(
            instructions, ensure_ascii=False, separators=(",", ":"),
        )
    instructions = {
        "task": "高质量复核主播 Reaction 中文字幕。逐条比较英文证据与 Round 1，只做有依据的最小修正。",
        "rules": [
            "删除 [music]、(音乐)、[音效] 等舞台提示；只返回说话内容",
            "english 是实际翻译源；primary/secondary 只用于纠正 ASR，冲突时不要拼接两句",
            "context_before/context_after 只帮助判断语境，绝对不要翻译进当前字幕",
            "以 Round 1 为锚，默认 keep；只有能指出明确英文证据和确定错误时才 replace",
            "忠实表达英文的主语、宾语、否定、数字和语气，不解释、不扩写",
            "使用自然简洁的 B 站主播口语，避免逐字硬译和书面腔",
            "required_name_terms 只包含当前条已确认的人名或专名；普通动作短语不得按人名解释，例如 pulling for 表示抽角色，绝不是卜灵",
            "risk_reasons 含 incomplete_fragment 时，可结合相邻上下文补足当前条被截断的语法成分，但不得重复翻译相邻条内容",
            "遇到明显 ASR 音近词时优先比较双源证据；没有独立证据就保留 Round 1 或给出保守最小修正，不得猜成其他游戏角色",
            "不得增删、合并、拆分或改变 id",
            "decision 只能是 keep 或 replace",
            "Round 1 的含义、语气和专名都准确时 decision=keep，text 必须原样返回 Round 1",
            "存在漏译、错译、英文残留、数字、否定或 required_name_terms 错误时必须 decision=replace",
            "replace 时只修正错误部分，不要为了润色而改写正确内容",
            "confidence 是 0 到 1 的数字；reason 必须说明具体错误和对应英文证据，不能只写优化表达",
            "results 必须逐一覆盖所有输入 items；即使 Round 1 正确也必须原样返回",
            f"本次输入 {len(payload)} 条，results 也必须恰好 {len(payload)} 条",
            "只输出一个 JSON 对象，不要 Markdown 代码块",
        ],
        "output_schema": {
            "results": [
                {
                    "id": "integer, must exactly match an input id",
                    "decision": "keep or replace",
                    "text": "non-empty Chinese subtitle",
                    "confidence": "number from 0 to 1",
                    "reason": "short Chinese justification",
                }
            ]
        },
        "glossary": glossary,
        "items": payload,
    }
    return json.dumps(instructions, ensure_ascii=False, separators=(",", ":"))


_AUTO_REPLACE_REASONS = AUTOMATIC_REVIEW_REASONS
_MOJIBAKE_RE = re.compile(r"[\u00c0-\u00ff]{2,}|(?:Ã|Â|æ|ç|å|Î|Ö|Ä|Ê|µ)")


def _requires_blocking_round_two(item: dict) -> bool:
    """Only deterministic, potentially auto-fixable risks block export.

    原因可能带证据后缀（如 `unknown_entity:Froolova`），按冒号前缀
    与自动审查集合匹配（2026-08-10 EN 实证：65 条风险因精确匹配
    失败全部 defer，Round 2 eligible=0）。
    """
    reasons = set(item.get("reasons") or [])
    prefixes = {str(reason).split(":", 1)[0] for reason in reasons}
    return bool(prefixes.intersection(_AUTO_REPLACE_REASONS))


def _merge_arbitration_risks(
    risks, candidates, round1_subtitles, data_dir, source_language,
    is_pure_music=None,
):
    """阶段 C：仲裁风险接入。

    - source_unresolved：两源严重冲突且无法仲裁 → 必须人工（BLOCKING 级）
    - unknown_entity：canonical 含未知专名（如 메카우터）→ 人工确认，不自动交付
    - hallucinated_entity：canonical 有未知专名且中文输出出现新词 → 疑似幻觉
    """
    from pipeline.preprocess.source_arbitration import (
        ArbitrationKnowledge, arbitrate_candidates, detect_hallucinated_entities,
    )
    from pipeline.risk_queue import RiskItem

    units = arbitrate_candidates(
        candidates, source_language=source_language, data_dir=data_dir,
    )
    if not units:
        return risks
    # Build canonical map: for ja/ko, build_risk_queue sets source_text to
    # preferred_source_text (primary evidence), but the canonical from
    # arbitration may differ. Update existing risk items so the Round 2
    # Reviewer sees the same source_text that was used for Round 1.
    canonical_map = {
        unit.key: unit.canonical_source_text
        for unit in units.values()
        if unit.canonical_source_text
    }
    if canonical_map:
        for item in risks:
            item_key = str(
                getattr(item, "key", None)
                or (item.get("key") if isinstance(item, dict) else "")
            ).strip()
            base_key = (
                item_key.split("arbitration:", 1)[-1]
                if item_key.startswith("arbitration:")
                else item_key
            )
            canonical = canonical_map.get(base_key)
            if canonical:
                if isinstance(item, dict):
                    item["source_text"] = canonical
                else:
                    item.source_text = canonical
    by_key = {candidate.key: candidate for candidate in candidates}
    translated_by_time = {
        (sub.start, sub.end): sub for sub in round1_subtitles
    }
    knowledge = ArbitrationKnowledge.from_data_dir(
        data_dir, game="wuwa", source_language=source_language,
    )
    existing_keys = {
        str(getattr(item, "key", None) or (item.get("key") if isinstance(item, dict) else "")).strip()
        for item in risks
    }
    added = []
    for unit in units.values():
        candidate = by_key.get(unit.key)
        if not candidate:
            continue
        source_text = unit.canonical_source_text or candidate.primary_evidence
        if is_pure_music is not None and is_pure_music(source_text):
            # Pure music/SFX rows are intentionally absent from Round 1.
            # Turning their missing translation into subtitle ID 0 creates a
            # blocking Round 2 item that no model can validly translate.
            continue
        translated = translated_by_time.get((candidate.start, candidate.end))
        chinese = translated.text if translated else ""
        reasons = []
        score = 0
        source_unreliable = (
            unit.source_decision == "unresolved"
            or (unit.source_conflict and unit.source_confidence < 0.4)
        )
        if source_unreliable:
            reasons.append("source_unresolved")
            score += 10
        hallucinated = []
        if chinese and unit.unknown_entities:
            # dry-run 占位符（[待翻译]）不参与幻觉检查
            if "待翻译" not in chinese:
                hallucinated = detect_hallucinated_entities(
                    chinese, unit, knowledge,
                )
        # unknown 风险只在源不可靠或中文疑似幻觉时升级（避免普通词噪声）
        if unit.unknown_entities and (source_unreliable or hallucinated):
            reasons.append(
                "unknown_entity:" + ",".join(unit.unknown_entities[:5])
            )
            score += 8
            if hallucinated:
                reasons.append(
                    "hallucinated_entity:" + ",".join(hallucinated[:5])
                )
                score += 10
        if not reasons:
            continue
        key = f"arbitration:{unit.key}"
        if key in existing_keys:
            continue
        added.append(RiskItem(
            key=key, subtitle_id=translated.id if translated else 0,
            start=candidate.start, end=candidate.end, score=score,
            reasons=sorted(set(reasons)),
            english=source_text,
            capcut_en=candidate.primary_evidence,
            whisper_en=candidate.secondary_evidence,
            translated=chinese,
            severity="high" if score >= 6 else "medium",
            source_language=source_language,
            source_text=source_text,
            primary_evidence=candidate.primary_evidence,
            secondary_evidence=candidate.secondary_evidence,
            review_required=True,
            needs_audio_evidence=bool(unit.source_conflict),
        ))
    if not added:
        return risks  # risks already updated with canonical above
    merged = list(risks) + added
    return sorted(merged, key=lambda item: (-item.score, item.start, item.key))


def _merge_english_entity_guard_risks(
    risks, candidates, round1_subtitles, data_dir, *,
    source_language="en", game="wuwa", is_pure_music=None,
):
    """Merge English Entity Guard evidence without entering source arbitration."""
    from pipeline.candidates import preferred_source_text
    from pipeline.entity_resolution import resolve_entity_candidates
    from pipeline.preprocess.source_arbitration import (
        ArbitrationKnowledge, ArbitratedUnit, detect_hallucinated_entities,
    )
    from pipeline.risk_queue import RiskItem

    knowledge = ArbitrationKnowledge.from_data_dir(
        data_dir, game=game, source_language=source_language,
    )
    translated_by_time = {
        (subtitle.start, subtitle.end): subtitle for subtitle in round1_subtitles
    }
    existing_keys = {
        str(getattr(item, "key", None) or (item.get("key") if isinstance(item, dict) else "")).strip()
        for item in risks
    }
    added = []
    candidate_list = list(candidates)
    for candidate_index, candidate in enumerate(candidate_list):
        source_text = preferred_source_text(candidate)
        if not source_text or (
            is_pure_music is not None and is_pure_music(source_text)
        ):
            continue
        discoveries = resolve_entity_candidates(
            source_text, knowledge.source_to_target,
            source_language=source_language,
        )
        unknowns = [
            item["surface"] for item in discoveries
            if item.get("classification") == "possible_entity"
        ]
        translated = translated_by_time.get((candidate.start, candidate.end))
        chinese = translated.text if translated else ""
        unit = ArbitratedUnit(
            key=candidate.key,
            start_ms=0,
            end_ms=0,
            source_language=source_language,
            primary_evidence=source_text,
            secondary_evidence=candidate.secondary_evidence,
            canonical_source_text=source_text,
            source_decision="canonical_english",
            source_confidence=1.0,
            source_conflict=False,
            unknown_entities=unknowns,
        )
        hallucinated = []
        if chinese and "待翻译" not in chinese:
            hallucinated = detect_hallucinated_entities(chinese, unit, knowledge)
        reasons = []
        score = 0
        if unknowns:
            reasons.append("unknown_entity:" + ",".join(unknowns[:5]))
            score += 8
        if hallucinated:
            reasons.append("hallucinated_entity:" + ",".join(hallucinated[:5]))
            score += 10
        if not reasons:
            continue
        key = f"entity_guard:{candidate.key}"
        if key in existing_keys:
            continue
        context_before = ""
        context_after = ""
        if candidate_index:
            context_before = preferred_source_text(
                candidate_list[candidate_index - 1]
            )
        if candidate_index + 1 < len(candidate_list):
            context_after = preferred_source_text(
                candidate_list[candidate_index + 1]
            )
        added.append(RiskItem(
            key=key,
            subtitle_id=translated.id if translated else 0,
            start=candidate.start,
            end=candidate.end,
            score=score,
            reasons=reasons,
            english=source_text,
            capcut_en=candidate.primary_evidence,
            whisper_en=candidate.secondary_evidence,
            translated=chinese,
            source_language=source_language,
            source_text=source_text,
            primary_evidence=candidate.primary_evidence,
            secondary_evidence=candidate.secondary_evidence,
            context_before=context_before,
            context_after=context_after,
            severity="high" if hallucinated else "medium",
            review_required=True,
        ))
    if not added:
        return risks
    return sorted(list(risks) + added, key=lambda item: (-item.score, item.start, item.key))


def _blocking_risk_keys(items) -> set[str]:
    """Return blocking keys from either generated RiskItem objects or dicts."""
    keys: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            payload = item
        else:
            to_dict = getattr(item, "to_dict", None)
            if not callable(to_dict):
                raise TypeError(
                    f"Unsupported risk item type: {type(item).__name__}"
                )
            payload = to_dict()
        key = str(payload.get("key", "")).strip()
        if key and _requires_blocking_round_two(payload):
            keys.add(key)
    return keys


def _evaluate_round_two_decision(
    item: dict,
    decision: dict,
    glossary: dict[str, str],
) -> dict:
    """Apply a deterministic gate before a model candidate may replace Round 1."""
    action = str(decision.get("decision", "")).lower()
    candidate = strip_music_annotations(str(decision.get("text", "")))
    round1_text = strip_music_annotations(str(item.get("translated", "")))
    confidence = float(decision.get("confidence", 0.0) or 0.0)
    reasons = set(item.get("reasons") or [])
    # 风险原因可能带证据后缀（unknown_entity:Froolova），前缀用于集合匹配
    prefixes = {str(reason).split(":", 1)[0] for reason in reasons}
    source_language = str(item.get("source_language", "en") or "en")
    rejection_reasons: list[str] = []

    if action == "keep":
        unresolved = prefixes.intersection(_AUTO_REPLACE_REASONS)
        if unresolved:
            return {
                "accepted": False,
                "applied": "round1",
                "text": strip_music_annotations(
                    str(item.get("translated", ""))
                ),
                "validation_reasons": [
                    f"keep_does_not_fix:{reason}"
                    for reason in sorted(unresolved)
                ],
            }
        return {
            "accepted": True,
            "applied": "round1",
            "text": strip_music_annotations(str(item.get("translated", ""))),
            "validation_reasons": [],
        }
    if action != "replace":
        rejection_reasons.append("legacy_or_invalid_decision")
    if confidence < ROUND_TWO_CONFIDENCE_THRESHOLD:
        rejection_reasons.append("low_confidence")
    if not reasons.intersection(_AUTO_REPLACE_REASONS):
        prefixes = {str(reason).split(":", 1)[0] for reason in reasons}
        if not prefixes.intersection(_AUTO_REPLACE_REASONS):
            rejection_reasons.append("weak_risk_only")
    if not candidate:
        rejection_reasons.append("empty_candidate")
    if candidate.strip() == round1_text.strip():
        rejection_reasons.append("replacement_unchanged")
    if _MOJIBAKE_RE.search(candidate):
        rejection_reasons.append("mojibake")
    if source_language == "en" and _has_english_residue(candidate):
        rejection_reasons.append("english_residue")
    if source_language == "ja" and re.search(r"[\u3040-\u30ff]", candidate):
        rejection_reasons.append("kana_residue")
    if source_language == "ko" and re.search(
        r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", candidate,
    ):
        rejection_reasons.append("hangul_residue")

    comparable_round1 = re.sub(r"[\W_]+", "", round1_text)
    comparable_candidate = re.sub(r"[\W_]+", "", candidate)
    # excessive_rewrite 只拦截"低置信或证据不足的大幅改写"：Round 1 本身
    # 错得离谱时，正确修复与错误原文差异大是必然（JA gl8vjOL1xWI 实证：
    # confidence 0.97-0.98 的正确修复曾被相似度 <0.3 硬拒）。豁免条件：
    # ①高置信（≥0.95）②reasons 含结构化可修原因（否定/数字/残缺等，
    # 证明 Round 1 有明确错误、修正有依据）。term_mismatch / 实体类原因
    # 不豁免——术语/人名修正应是小改，整句重写仍需相似度把关。
    _STRUCTURED_FIX_REASONS = {
        "negation_missing", "number_mismatch", "incomplete_fragment",
        "semantic_marker_missing", "empty_translation",
    }
    if (
        not (
            confidence >= ROUND_TWO_CONFIDENCE_THRESHOLD_HIGH
            and prefixes.intersection(_STRUCTURED_FIX_REASONS)
        )
        and "empty_translation" not in reasons
        and min(len(comparable_round1), len(comparable_candidate)) >= 6
        and difflib.SequenceMatcher(
            None, comparable_round1, comparable_candidate
        ).ratio() < 0.3
    ):
        rejection_reasons.append("excessive_rewrite")

    english = str(item.get("source_text", item.get("english", "")))
    source_numbers = _numbers(english)
    candidate_numbers = _numbers(candidate)
    if (
        "number_mismatch" in reasons
        and source_numbers
        and candidate_numbers
        and source_numbers != candidate_numbers
    ):
        rejection_reasons.append("number_validation_failed")
    required_terms = dict(item.get("term_hints") or {})
    if "term_mismatch" in reasons and not required_terms:
        # Legacy generated queues did not persist per-item hard terms.
        required_terms = _relevant_glossary([item], glossary)
    for term, chinese in required_terms.items():
        if term and chinese not in candidate:
            rejection_reasons.append(f"term_missing:{term}")
    # 幻觉名防护（2026-08-10 审计 + 2026-08-11 Phase 7 泛化）：
    # unknown_entity / hallucinated_entity 风险进自动 Reviewer 后，replace
    # 候选不得引入 glossary + term_hints 之外的新中文专名（"泽尔卡恩"类
    # 必须拒绝）。但 JA 长句实证（rerun-ja 30 个 replace_rejected）显示
    # 纯 re.findall 2~4 字 run + 静态词表过滤会把"也就是说/这个锚点/
    # 是完全/蕾姆塔洛"等普通短语误判为幻觉名，正确修复整句被拒。
    # 组合信号（不再无限追加静态词表）：
    #   ① 与 round1 相同的 run 不是"新引入"（蕾姆塔洛斯 round1 已有）
    #   ② 含高频虚词/功能字的 run 不可能是专名（"也就是说"含 也/说/是）
    #   ③ 与源文 unknown entity 音译对应的 run 不是幻觉（蕾姆塔洛斯=レムタロス）
    #   ④ glossary/term_hints 命中 → 已知，跳过
    #   ⑤ 其余才标记为疑似幻觉名
    entity_risk_prefixes = {
        reason.split(":", 1)[0] for reason in reasons
        if reason.startswith(("unknown_entity:", "hallucinated_entity:"))
    }
    if prefixes and prefixes.issubset({"unknown_entity", "hallucinated_entity"}):
        flagged_hallucinations = [
            surface.strip()
            for reason in reasons
            if reason.startswith("hallucinated_entity:")
            for surface in reason.split(":", 1)[1].split(",")
            if surface.strip()
        ]
        if flagged_hallucinations and all(
            surface in candidate for surface in flagged_hallucinations
        ):
            rejection_reasons.append("entity_risk_not_addressed")
    if entity_risk_prefixes:
        from pipeline.preprocess.source_arbitration import (
            _ZH_COMMON, _ZH_TRANSLITERATION_CHARS, _is_zh_transliteration,
        )
        known_zh = set(glossary.values()) | set(required_terms.values())
        source_all = " ".join(filter(None, (
            str(item.get("source_text", "") or ""),
            str(item.get("english", "") or ""),
            str(item.get("primary_evidence", "") or ""),
            str(item.get("secondary_evidence", "") or ""),
        )))
        candidate_has_round1 = candidate and round1_text
        for run in re.findall(r"[\u4e00-\u9fff]{2,4}", candidate):
            if run in known_zh:
                continue
            if any(common and (common in run or run in common) for common in _ZH_COMMON):
                continue
            # ① 与 round1 相同 → 不是新引入
            if candidate_has_round1 and run in round1_text:
                continue
            # ② 非音译形态（夹杂功能字）→ 普通短语，不是专名
            if not _is_zh_transliteration(run):
                continue
            # ③ 音译形态的新 run：若源文存在与 run 音译字重叠的假名
            #    专名词（同长度量级）→ 是 unknown 专名的合理音译，允许；
            #    否则（源文无对应音译词）→ 模型编造，拒绝。
            #    只有假名能做可靠音译字映射；韩文/拉丁词的音译不可预测
            #    （保守：新音译宁可拒绝进人工，不冒险放行——泽尔卡恩 与
            #    primary_evidence 里的韩文词无真实对应，必须拒绝）。
            def _source_translit_chars(word: str) -> set[str]:
                chars: set[str] = set()
                for ch in word:
                    if ch in _KANA_TO_ZH:
                        chars |= _KANA_TO_ZH[ch]
                return chars

            translit_source_words = re.findall(
                r"[\u3040-\u30ff]{2,12}", source_all,
            )
            allowed = False
            for word in translit_source_words:
                if not (len(run) <= len(word) * 2 and len(word) <= len(run) * 2):
                    continue
                source_chars = _source_translit_chars(word)
                if run and (set(run) & source_chars):
                    allowed = True
                    break
            if allowed:
                continue
            rejection_reasons.append(f"introduced_hallucinated_name:{run}")
    semantic_issues = _semantic_marker_issues(
        english, candidate, source_language,
    )
    source_has_negation = bool(re.search(
        r"\b(?:no|not|never|without|neither|nor|don'?t|doesn'?t|"
        r"didn'?t|isn'?t|aren'?t|wasn'?t|weren'?t|can'?t|won'?t)\b",
        english,
        re.IGNORECASE,
    ))
    negative_markers = ("不", "没", "未", "无", "别", "绝不", "从不")
    if source_language == "en" and (
        not source_has_negation
        and not any(marker in round1_text for marker in negative_markers)
        and any(marker in candidate for marker in negative_markers)
    ):
        rejection_reasons.append("introduced_negation")
    # For high-confidence corrections (≥ 0.95), skip semantic marker
    # validation — the heuristic can produce false positives (e.g. Korean
    # 없 in 없애다 "to eliminate" is not a negation), and a 95%+
    # confidence correction is more trustworthy than the heuristic.
    if confidence < 0.95 and (
        (
            "negation_missing" in reasons
            or "semantic_marker_missing" in reasons
        )
        and "negation_missing" in semantic_issues
    ):
        rejection_reasons.append("semantic_marker_validation_failed")
    if (
        "incomplete_fragment" in reasons
        and _is_incomplete_fragment(english, candidate, source_language)
    ):
        rejection_reasons.append("incomplete_fragment_validation_failed")

    try:
        from pipeline.parser.srt_parser import timestamp_to_ms

        duration = (
            timestamp_to_ms(str(item["end"]))
            - timestamp_to_ms(str(item["start"]))
        ) / 1000
    except (KeyError, TypeError, ValueError):
        duration = 0.0
    visible_characters = len(re.sub(r"\s+", "", candidate))
    if duration > 0 and visible_characters / duration > 15:
        rejection_reasons.append("reading_speed_validation_failed")

    source_residue_cleanup_for_review = False
    if set(rejection_reasons) == {"low_confidence"} and action == "replace":
        if (
            source_language == "ja"
            and "kana_residue" in prefixes
            and re.search(r"[\u3040-\u30ff]", round1_text)
            and not re.search(r"[\u3040-\u30ff]", candidate)
        ):
            source_residue_cleanup_for_review = True
        elif (
            source_language == "ko"
            and "hangul_residue" in prefixes
            and re.search(
                r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", round1_text,
            )
            and not re.search(
                r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", candidate,
            )
        ):
            source_residue_cleanup_for_review = True

    return {
        "accepted": not rejection_reasons,
        "applied": (
            "round2"
            if not rejection_reasons
            else "round2_review"
            if source_residue_cleanup_for_review
            else "round1"
        ),
        "text": (
            candidate
            if not rejection_reasons or source_residue_cleanup_for_review
            else strip_music_annotations(str(item.get("translated", "")))
        ),
        "validation_reasons": rejection_reasons,
    }


def _parse_round_two_response(
    response: str,
    expected_ids: set[int],
    *,
    allow_partial: bool = False,
) -> dict[int, dict]:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Round 2 response is not a JSON object")
    payload = json.loads(text[start:end + 1])
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Round 2 response is missing results[]")

    parsed: dict[int, dict] = {}
    unexpected: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            subtitle_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        translated = strip_music_annotations(str(item.get("text", "")))
        decision = str(item.get("decision", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = -1.0
        if subtitle_id not in expected_ids:
            unexpected.add(subtitle_id)
            continue
        if (
            subtitle_id in parsed
            or not translated
            or decision not in {"keep", "replace"}
            or not 0.0 <= confidence <= 1.0
            or not reason
        ):
            continue
        parsed[subtitle_id] = {
            "id": subtitle_id,
            "decision": decision,
            "text": translated,
            "confidence": confidence,
            "reason": reason,
        }
    if not allow_partial and set(parsed) != expected_ids:
        missing = sorted(expected_ids - set(parsed))
        raise ValueError(
            "Round 2 ids do not match; "
            f"missing={missing}, unexpected={sorted(unexpected)}"
        )
    return parsed


def _round_two_reusable_results(
    manifest: dict,
    active_items: list[dict],
) -> dict[int, dict]:
    """Keep reviewed outputs only when both subtitle ID and stable key match."""
    active_keys = {
        int(item["subtitle_id"]): str(item.get("key", ""))
        for item in active_items
    }
    old_keys = {
        int(item["id"]): str(item.get("key", ""))
        for item in manifest.get("entries", [])
        if item.get("id") is not None
    }
    legacy_schema = (
        str((manifest.get("options") or {}).get("schema", ""))
        != ROUND_TWO_SCHEMA
    )
    active_by_id = {
        int(item["subtitle_id"]): item for item in active_items
    }
    reusable: dict[int, dict] = {}
    for batch in (manifest.get("batches") or {}).values():
        for result in batch.get("results") or []:
            try:
                subtitle_id = int(result.get("id"))
            except (TypeError, ValueError):
                continue
            translated = str(result.get("translated", "")).strip()
            active = active_by_id.get(subtitle_id, {})
            # Old manifests have no language contract.  Never reuse one for
            # a Japanese risk queue: that would silently carry English-era
            # evidence/decisions into a different source language.
            language_source_changed = bool(
                legacy_schema
                and str(active.get("source_language", "en") or "en") != "en"
            )
            if (
                translated
                and subtitle_id in active_keys
                and old_keys.get(subtitle_id) == active_keys[subtitle_id]
                and not language_source_changed
            ):
                decision = str(result.get("decision", "")).lower()
                reusable[subtitle_id] = {
                    "id": subtitle_id,
                    "decision": (
                        decision if decision in {"keep", "replace"} else "review"
                    ),
                    "text": translated,
                    "confidence": float(result.get("confidence", 0.0) or 0.0),
                    "reason": str(
                        result.get("reason", "旧版 Round 2 候选，仅供人工比较")
                    ),
                }
    return reusable


def _create_round_two_manifest(
    input_path: str,
    options: dict,
    active_items: list[dict],
    reusable: dict[int, dict] | None = None,
) -> dict:
    """Create a Round 2 checkpoint, optionally seeding reviewed ID results."""
    reusable = reusable or {}
    normalised_reusable = {
        subtitle_id: (
            value
            if isinstance(value, dict)
            else {
                "id": subtitle_id,
                "decision": "review",
                "text": str(value),
                "confidence": 0.0,
                "reason": "旧版 Round 2 候选，仅供人工比较",
            }
        )
        for subtitle_id, value in reusable.items()
    }
    manifest = create_manifest(
        input_path,
        options,
        ({"id": int(item["subtitle_id"]), "key": item.get("key", "")}
         for item in active_items),
    )
    for batch_index, offset in enumerate(
        range(0, len(active_items), ROUND_TWO_BATCH_SIZE)
    ):
        batch = active_items[offset:offset + ROUND_TWO_BATCH_SIZE]
        ids = [int(item["subtitle_id"]) for item in batch]
        results = [
            {
                "id": subtitle_id,
                "translated": normalised_reusable[subtitle_id]["text"],
                "decision": normalised_reusable[subtitle_id]["decision"],
                "confidence": normalised_reusable[subtitle_id]["confidence"],
                "reason": normalised_reusable[subtitle_id]["reason"],
            }
            for subtitle_id in ids
            if subtitle_id in normalised_reusable
        ]
        set_batch(
            manifest,
            str(batch_index),
            "success" if len(results) == len(ids) else "pending",
            ids=ids,
            results=results,
            metrics={
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0.0,
                "elapsed_seconds": 0.0,
                "glossary_terms": 0,
                "response_attempts": 0,
                "migrated_results": len(results),
            },
        )
    manifest["metrics"] = {
        **summarize_manifest_metrics(manifest),
        "migrated_results": len(reusable),
        "seeded_batches": sum(
            bool(batch.get("results"))
            for batch in manifest.get("batches", {}).values()
        ),
    }
    return manifest


def _round_two_serial(
    items: list[dict],
    model: str,
    api_key: str,
    game: str,
    base_url: str = None,
    proxy: str = None,
    cancel_check=None,
    data_dir: str = None,
    manifest_path: str | None = None,
    input_path: str | None = None,
    resume: bool = False,
    evidence_fingerprint: str = "",
    translator=None,
    source_language: str = "en",
) -> dict[int, dict]:
    """Review risk subtitles using strict JSON and an independent manifest."""
    active_items = list(items)
    glossary = (
        _load_glossary(game, data_dir)
        if source_language == "en" else _load_glossary(
            game, data_dir, source_language=source_language,
        )
    )
    if translator is None:
        translator = create_translator(
            api_key=api_key,
            model=model,
            temperature=0.1,
            max_tokens=Config.LLM_MAX_TOKENS,
            base_url=base_url,
            proxy=proxy,
        )
    options = {
        "stage": "round2",
        "schema": ROUND_TWO_SCHEMA,
        "model": model,
        "provider": Config.provider_fingerprint(base_url),
        "game": game,
        "source_language": source_language,
        "batch_size": ROUND_TWO_BATCH_SIZE,
        "evidence_fingerprint": evidence_fingerprint,
        "llm_disable_thinking": thinking_policy_report(translator),
    }
    manifest = None
    if manifest_path and input_path:
        if resume and Path(manifest_path).exists():
            manifest = load_manifest(manifest_path)
            try:
                validate_resume(manifest, input_path, options)
            except ValueError:
                reusable = _round_two_reusable_results(manifest, active_items)
                backup = Path(manifest_path).with_suffix(
                    Path(manifest_path).suffix + ".before-rebase.bak"
                )
                if not backup.exists():
                    shutil.copy2(manifest_path, backup)
                manifest = _create_round_two_manifest(
                    input_path, options, active_items, reusable
                )
                save_manifest(manifest_path, manifest)
                log.info(
                    "Round 2 manifest rebased after risk/schema change; "
                    "preserved %d valid results",
                    len(reusable),
                )
        else:
            manifest = _create_round_two_manifest(
                input_path, options, active_items
            )
            save_manifest(manifest_path, manifest)

    decisions: dict[int, dict] = {}
    stage_started = time.time()
    total_batches = (len(active_items) + ROUND_TWO_BATCH_SIZE - 1) // ROUND_TWO_BATCH_SIZE
    for batch_index, offset in enumerate(
        range(0, len(active_items), ROUND_TWO_BATCH_SIZE),
        start=1,
    ):
        raise_if_cancelled(cancel_check)
        batch = active_items[offset:offset + ROUND_TWO_BATCH_SIZE]
        batch_id = str(batch_index - 1)
        expected_ids = {int(item["subtitle_id"]) for item in batch}
        if manifest:
            saved = successful_batch_results(manifest, batch_id)
            if saved:
                for item in saved:
                    subtitle_id = int(item["id"])
                    decision = str(item.get("decision", "")).lower()
                    decisions[subtitle_id] = {
                        "id": subtitle_id,
                        "decision": (
                            decision if decision in {"keep", "replace"} else "review"
                        ),
                        "text": str(item["translated"]),
                        "confidence": float(item.get("confidence", 0.0) or 0.0),
                        "reason": str(
                            item.get("reason", "旧版 Round 2 候选，仅供人工比较")
                        ),
                    }
                log.info("Round 2 batch %d/%d restored from manifest", batch_index, total_batches)
                continue
            reusable = partial_batch_results(manifest, batch_id)
            batch_corrections = {
                int(item["id"]): {
                    "id": int(item["id"]),
                    "decision": (
                        str(item.get("decision", "")).lower()
                        if str(item.get("decision", "")).lower()
                        in {"keep", "replace"}
                        else "review"
                    ),
                    "text": str(item["translated"]),
                    "confidence": float(item.get("confidence", 0.0) or 0.0),
                    "reason": str(
                        item.get("reason", "旧版 Round 2 候选，仅供人工比较")
                    ),
                }
                for item in reusable
                if int(item["id"]) in expected_ids
            }
            set_batch(
                manifest,
                batch_id,
                "running",
                ids=sorted(expected_ids),
                results=[
                    {
                        "id": subtitle_id,
                        "translated": result["text"],
                        "decision": result["decision"],
                        "confidence": result["confidence"],
                        "reason": result["reason"],
                    }
                    for subtitle_id, result in sorted(batch_corrections.items())
                ],
            )
            save_manifest(manifest_path, manifest)
        else:
            batch_corrections = {}

        relevant_terms = _relevant_glossary(batch, glossary)
        last_error: Exception | None = None
        tokens_in = 0
        tokens_out = 0
        response_attempts = 0
        batch_started = time.time()
        for attempt in range(2):
            pending = [
                item for item in batch
                if int(item["subtitle_id"]) not in batch_corrections
            ]
            if not pending:
                break
            pending_ids = {int(item["subtitle_id"]) for item in pending}
            prompt = _round_two_prompt(
                pending,
                _relevant_glossary(pending, glossary),
                source_language=source_language,
            )
            try:
                response_attempts += 1
                response, call_tokens_in, call_tokens_out = translator._call_api(prompt)
                tokens_in += int(call_tokens_in)
                tokens_out += int(call_tokens_out)
                raise_if_cancelled(cancel_check)
                parsed = _parse_round_two_response(
                    response,
                    pending_ids,
                    allow_partial=True,
                )
                pending_by_id = {
                    int(item["subtitle_id"]): item for item in pending
                }
                for subtitle_id, result in list(parsed.items()):
                    risk_prefixes = {
                        str(reason).split(":", 1)[0]
                        for reason in (
                            pending_by_id[subtitle_id].get("reasons") or []
                        )
                    }
                    translated = str(result.get("text", ""))
                    residue_issue = None
                    if source_language == "ja" and re.search(
                        r"[\u3040-\u30ff]", translated,
                    ):
                        residue_issue = "kana_residue"
                    elif source_language == "ko" and re.search(
                        r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]",
                        translated,
                    ):
                        residue_issue = "hangul_residue"
                    required_fix = risk_prefixes.intersection({
                        "kana_residue", "hangul_residue",
                    })
                    if (
                        residue_issue
                        or (
                            required_fix
                            and str(result.get("decision", "")).lower()
                            != "replace"
                        )
                    ):
                        parsed.pop(subtitle_id)
                        log.warning(
                            "Round 2 rejected subtitle #%d because source "
                            "script residue was not fixed (%s)",
                            subtitle_id,
                            residue_issue or "keep_for_residue_risk",
                        )
                batch_corrections.update(parsed)
                missing_ids = sorted(pending_ids - set(parsed))
                if manifest and parsed:
                    set_batch(
                        manifest,
                        batch_id,
                        "running",
                        ids=sorted(expected_ids),
                        results=[
                            {
                                "id": subtitle_id,
                                "translated": result["text"],
                                "decision": result["decision"],
                                "confidence": result["confidence"],
                                "reason": result["reason"],
                            }
                            for subtitle_id, result
                            in sorted(batch_corrections.items())
                        ],
                    )
                    save_manifest(manifest_path, manifest)
                if missing_ids:
                    last_error = ValueError(
                        f"Round 2 missing ids after attempt {attempt + 1}: "
                        f"{missing_ids}"
                    )
                    log.warning(
                        "Round 2 batch %d/%d accepted %d results; "
                        "retrying only %d missing ids: %s",
                        batch_index,
                        total_batches,
                        len(parsed),
                        len(missing_ids),
                        missing_ids,
                    )
                else:
                    last_error = None
                    break
            except PipelineCancelled:
                raise
            except LLMAPIError as error:
                fields = error.public_fields()
                if manifest:
                    set_batch(
                        manifest,
                        batch_id,
                        "failed",
                        ids=sorted(expected_ids),
                        results=[
                            {
                                "id": subtitle_id,
                                "translated": result["text"],
                                "decision": result["decision"],
                                "confidence": result["confidence"],
                                "reason": result["reason"],
                            }
                            for subtitle_id, result
                            in sorted(batch_corrections.items())
                        ],
                        error=fields["message"],
                        error_code=fields["error_code"],
                        upstream_status=fields["upstream_status"],
                        metrics={
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "cost": 0.0,
                            "elapsed_seconds": round(
                                time.time() - batch_started, 3
                            ),
                            "glossary_terms": len(relevant_terms),
                            "response_attempts": response_attempts,
                        },
                    )
                    manifest["status"] = "failed"
                    manifest["metrics"] = summarize_manifest_metrics(manifest)
                    save_manifest(manifest_path, manifest)
                raise
            except Exception as error:
                last_error = error
                log.warning(
                    "Round 2 batch %d/%d returned invalid structured output "
                    "(attempt %d/2); response details were discarded",
                    batch_index,
                    total_batches,
                    attempt + 1,
                )

        missing_ids = sorted(expected_ids - set(batch_corrections))
        if missing_ids:
            if manifest:
                fields = public_error_fields("round2_invalid_response")
                set_batch(
                    manifest,
                    batch_id,
                    "failed",
                    ids=sorted(expected_ids),
                    results=[
                        {
                            "id": subtitle_id,
                            "translated": result["text"],
                            "decision": result["decision"],
                            "confidence": result["confidence"],
                            "reason": result["reason"],
                        }
                        for subtitle_id, result
                        in sorted(batch_corrections.items())
                    ],
                    error=fields["message"],
                    error_code=fields["error_code"],
                    upstream_status=fields["upstream_status"],
                    metrics={
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "cost": 0.0,
                        "elapsed_seconds": round(
                            time.time() - batch_started, 3
                        ),
                        "glossary_terms": len(relevant_terms),
                        "response_attempts": response_attempts,
                    },
                )
                manifest["status"] = "partial"
                save_manifest(manifest_path, manifest)
            decisions.update(batch_corrections)
            continue

        decisions.update(batch_corrections)
        if manifest:
            estimate_cost = getattr(translator, "_estimate_cost", None)
            batch_cost = (
                float(estimate_cost(tokens_in, tokens_out))
                if callable(estimate_cost) else 0.0
            )
            set_batch(
                manifest,
                batch_id,
                "success",
                ids=sorted(expected_ids),
                results=[
                    {
                        "id": subtitle_id,
                        "translated": batch_corrections[subtitle_id]["text"],
                        "decision": batch_corrections[subtitle_id]["decision"],
                        "confidence": batch_corrections[subtitle_id]["confidence"],
                        "reason": batch_corrections[subtitle_id]["reason"],
                    }
                    for subtitle_id in sorted(batch_corrections)
                ],
                metrics={
                    "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out),
                    "cost": round(batch_cost, 8),
                    "elapsed_seconds": round(
                        time.time() - batch_started, 3
                    ),
                    "glossary_terms": len(relevant_terms),
                    "response_attempts": response_attempts,
                },
            )
            save_manifest(manifest_path, manifest)
        log.info("Round 2 batch %d/%d completed", batch_index, total_batches)

    if manifest:
        failed = any(
            batch.get("state") == "failed"
            for batch in manifest.get("batches", {}).values()
        )
        manifest["status"] = "partial" if failed else "completed"
        manifest["metrics"] = {
            **summarize_manifest_metrics(manifest),
            "parallelism": 1,
            "wall_seconds": round(
                time.time() - stage_started, 3
            ),
        }
        save_manifest(manifest_path, manifest)
    return decisions


def _round_two(
    items: list[dict],
    model: str,
    api_key: str,
    game: str,
    base_url: str = None,
    proxy: str = None,
    cancel_check=None,
    data_dir: str = None,
    manifest_path: str | None = None,
    input_path: str | None = None,
    resume: bool = False,
    evidence_fingerprint: str = "",
    source_language: str = "en",
) -> dict[int, dict]:
    """Run independent Round 2 batches two-at-a-time with safe 429 fallback."""
    active_items = list(items)
    if len(active_items) <= ROUND_TWO_BATCH_SIZE:
        return _round_two_serial(
            active_items, model, api_key, game,
            base_url=base_url,
            proxy=proxy,
            cancel_check=cancel_check,
            data_dir=data_dir,
            manifest_path=manifest_path,
            input_path=input_path,
            resume=resume,
            evidence_fingerprint=evidence_fingerprint,
            source_language=source_language,
        )

    # One translator owns one thread-safe httpx.Client, so all Round 2
    # requests share keep-alive connections instead of handshaking per batch.
    shared_translator = create_translator(
        api_key=api_key,
        model=model,
        temperature=0.1,
        max_tokens=Config.LLM_MAX_TOKENS,
        base_url=base_url,
        proxy=proxy,
    )
    preflight = getattr(shared_translator, "preflight", None)
    if callable(preflight):
        log.info("Round 2 API/model permission preflight")
        preflight()

    # A combined checkpoint already contains the authoritative ordering and
    # reusable results. Resume it serially to avoid reshuffling old work.
    if resume and manifest_path and Path(manifest_path).exists():
        return _round_two_serial(
            active_items, model, api_key, game,
            base_url=base_url,
            proxy=proxy,
            cancel_check=cancel_check,
            data_dir=data_dir,
            manifest_path=manifest_path,
            input_path=input_path,
            resume=True,
            evidence_fingerprint=evidence_fingerprint,
            translator=shared_translator,
            source_language=source_language,
        )

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    batches = [
        active_items[offset:offset + ROUND_TWO_BATCH_SIZE]
        for offset in range(0, len(active_items), ROUND_TWO_BATCH_SIZE)
    ]
    decisions: dict[int, dict] = {}
    batch_metrics: dict[int, dict] = {}
    part_paths: dict[int, Path] = {}
    rate_limited = False
    fatal_error: BaseException | None = None
    stage_started = time.time()

    def part_manifest(index: int) -> str | None:
        if not manifest_path:
            return None
        path = Path(f"{manifest_path}.part-{index:04d}")
        part_paths[index] = path
        return str(path)

    def run_batch(index: int) -> tuple[int, dict[int, dict], dict]:
        part_path = part_manifest(index)
        result = _round_two_serial(
            batches[index],
            model,
            api_key,
            game,
            base_url=base_url,
            proxy=proxy,
            cancel_check=cancel_check,
            data_dir=data_dir,
            manifest_path=part_path,
            input_path=input_path,
            resume=True,
            evidence_fingerprint=evidence_fingerprint,
            translator=shared_translator,
            source_language=source_language,
        )
        metrics: dict = {}
        if part_path and Path(part_path).exists():
            part = load_manifest(part_path)
            metrics = dict(
                (part.get("batches", {}).get("0", {}).get("metrics") or {})
            )
        return index, result, metrics

    pool = ThreadPoolExecutor(max_workers=2)
    pending = {}
    next_index = 0
    try:
        while next_index < len(batches) and len(pending) < 2:
            future = pool.submit(run_batch, next_index)
            pending[future] = next_index
            next_index += 1
        while pending:
            raise_if_cancelled(cancel_check)
            completed, _ = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                pending.pop(future, None)
                try:
                    index, result, metrics = future.result()
                    decisions.update(result)
                    batch_metrics[index] = metrics
                except LLMTransientError as error:
                    if error.status_code == 429:
                        rate_limited = True
                        log.warning(
                            "Round 2 provider rate-limited concurrent requests; "
                            "remaining batches will run serially"
                        )
                    else:
                        fatal_error = error
                except BaseException as error:
                    fatal_error = error
            if not rate_limited and fatal_error is None:
                while next_index < len(batches) and len(pending) < 2:
                    future = pool.submit(run_batch, next_index)
                    pending[future] = next_index
                    next_index += 1
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)

    options = {
        "stage": "round2",
        "schema": ROUND_TWO_SCHEMA,
        "model": model,
        "game": game,
        "batch_size": ROUND_TWO_BATCH_SIZE,
        "evidence_fingerprint": evidence_fingerprint,
        "llm_disable_thinking": thinking_policy_report(shared_translator),
    }

    def save_combined_manifest() -> None:
        if not manifest_path or not input_path:
            return
        combined = _create_round_two_manifest(
            input_path,
            options,
            active_items,
            decisions,
        )
        for index, metrics in batch_metrics.items():
            batch = combined.get("batches", {}).get(str(index))
            if batch is not None:
                batch["metrics"] = dict(metrics)
        combined["metrics"] = {
            **summarize_manifest_metrics(combined),
            "parallelism": 2,
            "rate_limit_downgraded": rate_limited,
            "wall_seconds": round(time.time() - stage_started, 3),
        }
        save_manifest(manifest_path, combined)

    save_combined_manifest()
    if fatal_error is not None:
        raise fatal_error

    if rate_limited or next_index < len(batches):
        result = _round_two_serial(
            active_items,
            model,
            api_key,
            game,
            base_url=base_url,
            proxy=proxy,
            cancel_check=cancel_check,
            data_dir=data_dir,
            manifest_path=manifest_path,
            input_path=input_path,
            resume=bool(manifest_path and input_path),
            evidence_fingerprint=evidence_fingerprint,
            translator=shared_translator,
            source_language=source_language,
        )
        if manifest_path and Path(manifest_path).exists():
            combined = load_manifest(manifest_path)
            combined.setdefault("metrics", {}).update({
                "parallelism": 1,
                "rate_limit_downgraded": True,
            })
            save_manifest(manifest_path, combined)
        decisions = result

    if manifest_path and Path(manifest_path).exists():
        for part_path in part_paths.values():
            part_path.unlink(missing_ok=True)
            Path(str(part_path) + ".before-rebase.bak").unlink(missing_ok=True)
    return decisions


def _collect_local_evidence(
    video_path: str,
    risk_path: str,
    results_path: str,
    evidence_dir: str,
    model: str,
    cancel_check=None,
    include_keys: set[str] | None = None,
    source_language: str = "en",
) -> None:
    """Load Whisper once and transcribe all audio-dependent risk windows."""
    generated_items = load_generated_risks(risk_path)
    results_payload = load_round2_results(results_path)
    result_by_key = {
        item["key"]: dict(item)
        for item in results_payload.get("items", [])
        if item.get("key")
    }
    evidence_items = [
        item
        for item in generated_items
        if item.get("needs_audio_evidence", False)
        and (
            include_keys is None
            or str(item.get("key", "")) in include_keys
        )
    ]
    windows = build_windows(evidence_items)
    if not windows:
        log.info("No risk item needs local audio evidence")
        return

    evidence_path = Path(evidence_dir)
    evidence_path.mkdir(parents=True, exist_ok=True)
    windows_path = evidence_path / "windows.json"
    specifications = []
    for index, window in enumerate(windows, start=1):
        specifications.append({
            "index": index,
            "start": window.start_ms / 1000,
            "end": window.end_ms / 1000,
            "output": str(evidence_path / f"risk-window-{index:03d}.whisper.srt"),
        })
    windows_path.write_text(
        json.dumps(specifications, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    script = PROJECT_ROOT / "pipeline" / "tools" / "whisper_transcribe.py"
    command = [
        sys.executable,
        str(script),
        video_path,
        "--windows-json",
        str(windows_path),
        "--model",
        model,
        "--language",
        normalize_language_pair({"source_language": source_language})[
            "source_language"
        ],
        "--beam-size",
        "5",
    ]
    returncode = _run_cancellable(command, cancel_check)
    raise_if_cancelled(cancel_check)

    for window, specification in zip(windows, specifications):
        output = Path(specification["output"])
        evidence = str(output) if returncode == 0 and output.exists() else ""
        evidence_text = ""
        if evidence:
            try:
                evidence_text = " ".join(
                    subtitle.text for subtitle in parse_srt(evidence)
                )
            except (OSError, ValueError):
                evidence = ""
        for key in window.keys:
            item = result_by_key.setdefault(key, {"key": key})
            item["local_evidence"] = evidence
            item["local_evidence_text"] = evidence_text
            item["state"] = "pending" if evidence else "failed"
            item["local_error"] = "" if evidence else f"local whisper exit {returncode}"
    save_round2_results(
        results_path,
        result_by_key.values(),
        metrics=results_payload.get("metrics", {}),
        risk_sha256=file_sha256(risk_path),
    )


def _run_cancellable(command: list[str], cancel_check=None, emit=None) -> int:
    """Stream a child process through logging and stop its process group on cancel."""
    options = {
        "cwd": PROJECT_ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    def stop_process() -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()

    while True:
        if cancel_check and cancel_check():
            stop_process()
            raise PipelineCancelled("Task cancelled")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                break
            continue
        if line is None:
            break
        if line.strip():
            message = f"Whisper: {line.strip()}"
            log.info(message)
            if emit is not None:
                emit(message)

    process.wait()
    reader.join(timeout=1)
    return process.returncode


def _atomic_json(path: str, payload: dict) -> None:
    target = Path(path)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, target)


def _evidence_fingerprint(items: list[dict]) -> str:
    evidence = [{
        "key": item.get("key", ""),
        "local_evidence_text": item.get("local_evidence_text", ""),
        "local_error": item.get("local_error", ""),
    } for item in items]
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_metrics(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return dict(load_manifest(path).get("metrics") or {})


def _write_pipeline_metrics(
    paths: dict[str, str],
    candidates: list,
    risks: list,
    primary_count: int,
    secondary_count: int,
) -> dict:
    dual_source_matches = sum(
        bool(item.capcut_en.strip() and item.whisper_en.strip())
        for item in candidates
    )
    risk_count = len(risks)
    review_required_count = sum(
        bool(
            item.get("review_required", False)
            if isinstance(item, dict)
            else item.review_required
        )
        for item in risks
    )
    audio_count = sum(
        bool(
            item.get("needs_audio_evidence", False)
            if isinstance(item, dict)
            else item.needs_audio_evidence
        )
        for item in risks
    )
    union_count = len(candidates)
    display_metrics = {}
    if Path(paths["final"]).is_file():
        from pipeline.postprocess.validate import validate_display_cues
        final_cues = parse_srt(paths["final"])
        dry_run_placeholders = any(
            str(item.text).lstrip().startswith("[待翻译]")
            for item in final_cues
        )
        display_report = validate_display_cues(
            final_cues, raise_on_error=False,
            allow_source_residue=dry_run_placeholders,
        )
        durations = [
            timestamp_to_ms(item.end) - timestamp_to_ms(item.start)
            for item in final_cues
        ]
        from pipeline.preprocess.cue_classifier import (
            has_cjk_content, is_pure_sfx_annotation,
        )
        untranslated_source_cues = sum(
            1 for item in final_cues
            if str(item.text or "").strip()
            and not has_cjk_content(str(item.text))
            and not is_pure_sfx_annotation(str(item.text))
        )
        display_metrics = {
            "display_cues": len(final_cues),
            "display_errors": len(display_report.errors),
            "display_warnings": len(display_report.warnings),
            "short_display_rate": round(
                sum(duration < 1_000 for duration in durations)
                / max(len(durations), 1), 4,
            ),
            "long_display_rate": round(
                sum(duration > 7_000 for duration in durations)
                / max(len(durations), 1), 4,
            ),
            "untranslated_source_cues": untranslated_source_cues,
        }
    payload = {
        "schema_version": 1,
        "round1": _manifest_metrics(paths["round1_manifest"]),
        "round2": _manifest_metrics(paths["round2_manifest"]),
        "quality": {
            "primary_subtitles": primary_count,
            "secondary_subtitles": secondary_count,
            "union_subtitles": union_count,
            "dual_source_matches": dual_source_matches,
            "dual_source_match_rate": round(
                dual_source_matches / max(union_count, 1), 4
            ),
            "risk_count": risk_count,
            "risk_rate": round(risk_count / max(union_count, 1), 4),
            "review_required_count": review_required_count,
            "review_required_rate": round(
                review_required_count / max(union_count, 1), 4
            ),
            "advisory_count": risk_count - review_required_count,
            "audio_evidence_count": audio_count,
            "audio_evidence_rate": round(
                audio_count / max(risk_count, 1), 4
            ),
            **display_metrics,
        },
    }
    _atomic_json(paths["metrics"], payload)
    return payload


def _apply_accepted_reconstructions(
    union_path: str,
    units: dict,
    rebuilt: dict,
) -> tuple[int, set[str]]:
    """将 acceptance gate 通过的 canonical_after 原子式写回 Round1 union。"""
    union_subs = parse_srt(union_path)
    by_time = {
        (unit.start_ms, unit.end_ms): unit for unit in units.values()
    }
    replaced = 0
    applied_keys: set[str] = set()
    for sub in union_subs:
        unit = by_time.get((
            timestamp_to_ms(sub.start), timestamp_to_ms(sub.end),
        ))
        entry = rebuilt.get(unit.key) if unit else None
        if (
            entry
            and entry.get("acceptance") == "accepted"
            and entry.get("text")
        ):
            sub.text = entry["text"]
            replaced += 1
            applied_keys.add(unit.key)
    save_srt(union_path, union_subs)
    return replaced, applied_keys


def run_long_video(
    capcut_en: str,
    whisper_en: str,
    output: str,
    model: str,
    api_key: str,
    batch_size: int,
    game: str,
    resume: bool,
    video: str | None = None,
    local_whisper: bool = False,
    dry_run: bool = False,
    base_url: str = None,
    proxy: str = None,
    cancel_check=None,
    progress_callback=None,
    data_dir: str = None,
    round2_model: str | None = None,
    video_context: dict | None = None,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> int:
    language_pair = normalize_language_pair({
        "source_language": source_language,
        "target_language": target_language,
    })
    source_language = language_pair["source_language"]
    target_language = language_pair["target_language"]
    round2_model = str(round2_model or model).strip()
    paths = _stage_paths(output, source_language)
    raise_if_cancelled(cancel_check)
    candidates, primary_count, secondary_count = _write_union(
        capcut_en, whisper_en, paths["union"], source_language=source_language,
        display_map_path=paths["display_map"],
    )
    log.info(
        "%s union completed: %d entries -> %s",
        source_language, len(candidates), Path(paths["union"]).name,
    )

    from pipeline.main import run_pipeline

    # 阶段 D：高风险源文本重建（仅 unresolved/低置信冲突句；en/dry-run/resume 保护）
    if source_language in {"ja", "ko"} and not dry_run and (
        not resume or not Path(paths["round1"]).exists()
    ):
        from pipeline.preprocess.source_arbitration import arbitrate_candidates
        from pipeline.preprocess.source_reconstruction import (
            reconstruct_sources, should_reconstruct, update_trace_applied,
        )
        from pipeline.translate.llm import OpenAICompatibleTranslator
        units = arbitrate_candidates(
            candidates, source_language=source_language, data_dir=data_dir,
        )
        high_risk = [u for u in units.values() if should_reconstruct(u)]
        if high_risk:
            log.info(
                "Source reconstruction: %d high-risk units (cap %d)",
                len(high_risk), 25,
            )
            translator = OpenAICompatibleTranslator(
                api_key=api_key, model=model,
                base_url=base_url, proxy=proxy,
            )
            rebuilt = reconstruct_sources(
                units, candidates, source_language=source_language,
                data_dir=data_dir, translator=translator, max_units=25,
                trace_path=paths["reconstruction_trace"],
            )
            if rebuilt:
                replaced, applied_keys = _apply_accepted_reconstructions(
                    paths["union"], units, rebuilt,
                )
                # 更新 trace 中的 applied 字段
                update_trace_applied(
                    paths["reconstruction_trace"], applied_keys,
                )
                log.info(
                    "Source reconstruction: replaced %d/%d union rows",
                    replaced, len(rebuilt),
                )
            else:
                log.info(
                    "Source reconstruction: 0 resolved results "
                    "(trace still saved at %s)",
                    paths["reconstruction_trace"],
                )
        else:
            log.info("Source reconstruction: 0 high-risk units eligible")

    cue_classes: dict[int, CueClass] = {}
    round1_input = paths["union"]
    round1_output = paths["round1"]
    if source_language == "en":
        cue_classes = _prepare_en_translation_input(
            paths["union"], paths["translation_input"], data_dir=data_dir,
        )
        round1_input = paths["translation_input"]
        round1_output = paths["round1_translation"]
        class_counts = {
            cue_class.value: sum(
                value == cue_class for value in cue_classes.values()
            )
            for cue_class in CueClass
        }
        log.info("English cue classification: %s", class_counts)

    has_translatable_cues = (
        source_language != "en"
        or CueClass.ENGLISH_REACTION in cue_classes.values()
    )
    if has_translatable_cues:
        status = run_pipeline(
            round1_input,
            round1_output,
            model=model,
            api_key=api_key,
            batch_size=batch_size,
            game=game,
            term_anchor=True,
            manifest_path=paths["round1_manifest"],
            resume=resume,
            dry_run=dry_run,
            dedupe=False,
            preserve_skipped=False,
            base_url=base_url,
            proxy=proxy,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            data_dir=data_dir,
            video_context=video_context,
            source_language=source_language,
            target_language=target_language,
        )
        if status:
            return status

    if source_language == "en":
        _merge_en_classified_round1(
            paths["union"],
            round1_output if has_translatable_cues else None,
            paths["round1"],
            cue_classes,
        )

    raise_if_cancelled(cancel_check)
    round1_subtitles = parse_srt(paths["round1"])
    music_detector = load_music_detector(data_dir)
    risk_evidence = _all_source_risk_evidence(candidates)
    if source_language == "en":
        risk_glossary = _add_fuzzy_name_hints(
            _load_review_glossary(game, data_dir), risk_evidence, game, data_dir,
        )
        known_people = _load_person_glossary(game, data_dir)
    else:
        risk_glossary = _add_fuzzy_name_hints(
            _load_review_glossary(
                game, data_dir, source_language=source_language,
            ), risk_evidence, game, data_dir, source_language=source_language,
        )
        known_people = _load_person_glossary(
            game, data_dir, source_language=source_language,
        )
    person_chinese = set(known_people.values())
    person_glossary = {
        **known_people,
        **{
            english: chinese
            for english, chinese in risk_glossary.items()
            if chinese in person_chinese
        },
    }
    person_glossary.update(
        _source_matched_person_aliases(
            known_people,
            risk_evidence,
            data_dir,
            source_language=source_language,
        )
    )
    risks = build_risk_queue(
        candidates,
        round1_subtitles,
        risk_glossary,
        person_glossary,
        music_detector.is_pure_music,
        music_detector.strip_markers,
        source_language=source_language,
    )
    if source_language == "en":
        risks = _merge_cue_classification_risks(
            risks, candidates, round1_subtitles, cue_classes,
        )
        risks = _merge_english_entity_guard_risks(
            risks,
            candidates,
            round1_subtitles,
            data_dir,
            source_language=source_language,
            game=game,
            is_pure_music=music_detector.is_pure_music,
        )
    # ja/ko：仲裁风险接入（source_unresolved / unknown_entity / hallucinated_entity）
    if source_language in {"ja", "ko"}:
        risks = _merge_arbitration_risks(
            risks, candidates, round1_subtitles, data_dir, source_language,
            is_pure_music=music_detector.is_pure_music,
        )
    save_generated_risks(paths["risk_generated"], risks)
    ensure_review_state(paths["review_state"])
    if not resume or not Path(paths["round2_results"]).exists():
        save_round2_results(
            paths["round2_results"],
            [],
            risk_sha256=file_sha256(paths["risk_generated"]),
        )
    log.info(
        "Generated risks: %d entries -> %s",
        len(risks),
        paths["risk_generated"],
    )

    if local_whisper:
        if not video:
            raise ValueError("--local-whisper requires --video")
        blocking_keys = _blocking_risk_keys(risks)
        _collect_local_evidence(
            video,
            paths["risk_generated"],
            paths["round2_results"],
            paths["evidence_dir"],
            "medium",
            cancel_check=cancel_check,
            include_keys=blocking_keys,
            source_language=source_language,
        )

    if dry_run:
        generated_items = load_generated_risks(paths["risk_generated"])
        existing = load_round2_results(paths["round2_results"])
        existing_by_key = {
            item["key"]: dict(item)
            for item in existing.get("items", [])
            if item.get("key")
        }
        for item in generated_items:
            result = existing_by_key.setdefault(item["key"], {"key": item["key"]})
            result["state"] = "review"
            result["round2_status"] = "deferred_weak_risk"
        save_round2_results(
            paths["round2_results"],
            existing_by_key.values(),
            risk_sha256=file_sha256(paths["risk_generated"]),
        )
        save_srt(paths["round2"], round1_subtitles)
        _publish_final(
            paths["round2"], paths["final"], paths["display_map"],
            allow_source_residue=True,
        )
        _write_pipeline_metrics(
            paths, candidates, risks, primary_count, secondary_count
        )
        log.info("Dry run completed; Round 2 LLM was skipped")
        return 0

    generated_items = load_generated_risks(paths["risk_generated"])
    round2_payload = load_round2_results(paths["round2_results"])
    queue_items = merge_risk_views(
        generated_items,
        round2_payload.get("items", []),
    )
    strong_queue_items = [
        item for item in queue_items
        if _requires_blocking_round_two(item)
    ]
    deferred_count = len(queue_items) - len(strong_queue_items)
    log.info(
        "Round 2 blocking queue: %d strong risks; %d weak risks deferred",
        len(strong_queue_items),
        deferred_count,
    )
    decisions = _round_two(
        strong_queue_items,
        round2_model,
        api_key,
        game,
        base_url=base_url,
        proxy=proxy,
        cancel_check=cancel_check,
        data_dir=data_dir,
        manifest_path=paths["round2_manifest"],
        input_path=paths["risk_generated"],
        resume=resume,
        evidence_fingerprint=_evidence_fingerprint(strong_queue_items),
        source_language=source_language,
    )
    raise_if_cancelled(cancel_check)

    glossary = (
        _load_glossary(game, data_dir)
        if source_language == "en" else _load_glossary(
            game, data_dir, source_language=source_language,
        )
    )
    decision_outcomes: dict[int, dict] = {}
    publishable_replacements: dict[int, str] = {}
    for item in generated_items:
        subtitle_id = int(item["subtitle_id"])
        decision = decisions.get(subtitle_id)
        if decision is None:
            continue
        outcome = _evaluate_round_two_decision(item, decision, glossary)
        decision_outcomes[subtitle_id] = outcome
        if outcome["applied"] in {"round2", "round2_review"}:
            publishable_replacements[subtitle_id] = outcome["text"]

    round2_subtitles = [
        type(subtitle)(
            subtitle.id,
            subtitle.start,
            subtitle.end,
            strip_music_annotations(
                publishable_replacements.get(subtitle.id, subtitle.text)
            ),
        )
        for subtitle in round1_subtitles
    ]
    save_srt(paths["round2"], round2_subtitles)
    _publish_final(paths["round2"], paths["final"], paths["display_map"])

    active_keys = {
        str(item.get("key")) for item in generated_items if item.get("key")
    }
    result_by_key = {
        item["key"]: dict(item)
        for item in round2_payload.get("items", [])
        if item.get("key") and str(item.get("key")) in active_keys
    }
    for item in generated_items:
        result = result_by_key.setdefault(item["key"], {"key": item["key"]})
        subtitle_id = int(item["subtitle_id"])
        is_blocking = _requires_blocking_round_two(item)
        if subtitle_id in decisions:
            decision = decisions[subtitle_id]
            outcome = decision_outcomes[subtitle_id]
            result.update({
                "round2_text": decision["text"],
                "decision": decision["decision"],
                "confidence": decision["confidence"],
                "reason": decision["reason"],
                "accepted": outcome["accepted"],
                "applied": outcome["applied"],
                "validation_reasons": outcome["validation_reasons"],
            })
            result["state"] = (
                "resolved" if outcome["accepted"] else "review"
            )
            dec = str(decision["decision"]).lower()
            if dec == "keep":
                result["round2_status"] = "keep"
            elif dec == "replace" and outcome["accepted"]:
                result["round2_status"] = "replace"
            elif dec == "replace" and not outcome["accepted"]:
                result["round2_status"] = "replace_rejected"
            else:
                result["round2_status"] = "unresolved"
        elif is_blocking:
            # Item was eligible for Round 2 but got no result (parse/API error)
            result["state"] = (
                result.get("state", "resolved")
                if result.get("round2_text")
                else "on_demand"
            )
            result["round2_status"] = "no_result"
        else:
            # Weak risk: not eligible for Round 2, deferred to human review
            result["state"] = (
                result.get("state", "resolved")
                if result.get("round2_text")
                else "on_demand"
            )
            result["round2_status"] = "deferred_weak_risk"

    if Path(paths["round2_manifest"]).exists():
        manifest = load_manifest(paths["round2_manifest"])
        manifest.setdefault("artifacts", {})["output"] = {
            "path": Path(paths["round2"]).name,
            "sha256": file_sha256(paths["round2"]),
        }
        save_manifest(paths["round2_manifest"], manifest)

    round2_metrics = _manifest_metrics(paths["round2_manifest"])
    # Build round2_summary with unambiguous status counts
    status_counts = {}
    for r in result_by_key.values():
        status = r.get("round2_status", "deferred_weak_risk")
        status_counts[status] = status_counts.get(status, 0) + 1
    round2_metrics["round2_summary"] = {
        "risk_total": len(generated_items),
        "round2_eligible": sum(
            1 for item in generated_items
            if _requires_blocking_round_two(item)
        ),
        **{k: status_counts.get(k, 0) for k in [
            "keep", "replace", "replace_rejected",
            "unresolved", "no_result", "deferred_weak_risk",
        ]},
    }
    save_round2_results(
        paths["round2_results"],
        result_by_key.values(),
        metrics=round2_metrics,
        risk_sha256=file_sha256(paths["risk_generated"]),
    )
    _write_pipeline_metrics(
        paths, candidates, risks, primary_count, secondary_count
    )

    resolved_count = sum(
        1 for item in result_by_key.values() if item.get("state") == "resolved"
    )
    review_count = sum(
        1 for item in result_by_key.values() if item.get("state") == "review"
    )
    log.info(
        "Round 2 completed: %d resolved, %d require review -> %s",
        resolved_count,
        review_count,
        paths["round2_results"],
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Long-video subtitle translation with two same-language sources",
    )
    parser.add_argument(
        "--primary-srt", "--capcut-en", dest="capcut_en", required=True,
        help="Primary source-language SRT",
    )
    parser.add_argument(
        "--secondary-srt", "--whisper-en", dest="whisper_en", required=True,
        help="Secondary source-language SRT",
    )
    parser.add_argument("-o", "--output", required=True, help="Published Chinese SRT")
    parser.add_argument("--model", default=Config.LLM_MODEL)
    parser.add_argument(
        "--round2-model",
        default=None,
        help="Model used for Round 2 risk review; defaults to --model",
    )
    parser.add_argument("--api-key", default=Config.LLM_API_KEY)
    parser.add_argument(
        "--proxy",
        default=None,
        help="LLM HTTP/HTTPS proxy, for example http://127.0.0.1:7890",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--game", default="wuwa")
    parser.add_argument("--source-language", choices=("en", "ja"), default="en")
    parser.add_argument("--target-language", choices=("zh-CN",), default="zh-CN")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--video", help="Video/audio used for local risk evidence")
    parser.add_argument(
        "--local-whisper",
        action="store_true",
        help="Collect local Whisper evidence only for audio-dependent risks",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate orchestration without calling an LLM",
    )
    args = parser.parse_args()
    if not args.api_key and not args.dry_run:
        parser.error("Set LLM_API_KEY or pass --api-key")
    sys.exit(run_long_video(**vars(args)))


if __name__ == "__main__":
    main()
