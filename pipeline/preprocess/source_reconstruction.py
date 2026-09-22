"""高风险源文本重建（Source Reconstruction）。

设计目标（结构性升级提示词阶段 D）：
- 只对仲裁后无法可靠定稿的高风险句（unresolved / 低置信冲突 / 未知专名冲突）
  调用一次 LLM，任务是从多路 ASR + 上下文 + 术语表恢复"最可能的韩语原句"，
  而不是翻译成中文。
- 硬约束：禁止凭空创造角色 / 禁止根据中文剧情反向编造 / 无法判断输出 unresolved。
- 成本控制：max_units 上限；只有 should_reconstruct 命中才调用。

输出格式：{key: {"text": 重建结果或空, "decision": "reconstructed"|"unresolved"|"skipped"}}
"""
from __future__ import annotations

import json
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.preprocess.source_arbitration import (
    ArbitratedUnit, ArbitrationKnowledge, _KO_STOPWORDS,
    _extract_entity_candidates, _is_fragment, _normalise_for_compare,
)

log = logging.getLogger("source_reconstruction")

# 韩文音节 + 日文假名（重建结果合法性检查用）
_SOURCE_SCRIPT_RE = re.compile(r"[\uac00-\ud7af\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION_MARKERS = {
    "ko": ("안", "못", "않", "없", "아니", "말"),
    "ja": ("ない", "無い", "ぬ", "ません", "じゃない", "ではない"),
}
_RELATIONSHIP_MARKERS = {
    "ko": ("엄마", "아빠", "어머니", "아버지", "언니", "오빠", "누나", "형", "동생", "친구", "딸", "아들"),
    "ja": ("母", "父", "姉", "兄", "妹", "弟", "友達", "娘", "息子"),
}


def _language_label(source_language: str) -> str:
    """返回源语言的中文标签。"""
    if source_language == "ja":
        return "日语"
    if source_language == "ko":
        return "韩语"
    return source_language


def should_reconstruct(unit: ArbitratedUnit) -> bool:
    """触发条件：无法仲裁 / 低置信冲突 / 未知专名伴随源冲突。"""
    if unit.source_decision == "unresolved":
        return True
    if unit.source_conflict and unit.source_confidence < 0.45:
        return True
    if unit.unknown_entities and unit.source_conflict:
        return True
    return False


def _format_section(rows: List[str], fallback: str = "（无）") -> str:
    return "\n".join(rows) if rows else fallback


def build_reconstruction_prompt(
    unit: ArbitratedUnit,
    before: List[str],
    after: List[str],
    knowledge: ArbitrationKnowledge,
) -> str:
    """构建重建 prompt（输出韩语原句，不翻译）。"""
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "prompts",
        f"source-reconstruct-{knowledge.source_language}.txt",
    )
    if os.path.isfile(template_path):
        template = open(template_path, encoding="utf-8-sig").read()
    else:
        template = (
            "你是{lang} ASR 修复专家。\n"
            "恢复最可能的{lang}原句（不是翻译）。\n"
            "【上下文】\n前文：\n{before_section}\n"
            "本句：\n- 版本A：{primary}\n- 版本B：{secondary}\n后文：\n{after_section}\n"
            "【规则】禁止编造角色；无法判断输出 unresolved。\n"
            "只输出重建后的{lang}原句。"
        )

    # 术语表（按本句证据命中的优先，最多 15 条）
    glossary_rows = []
    try:
        from pipeline.preprocess.glossary import load_glossary_db
        database = load_glossary_db(None)
        evidence = f"{unit.primary_evidence} {unit.secondary_evidence}"
        hits = database.extract_from_text(
            evidence, game="wuwa",
            source_language=knowledge.source_language,
        )
        for source, target in list(hits.items())[:15]:
            glossary_rows.append(f"{source} = {target}")
    except Exception:
        pass
    if not glossary_rows:
        for term in sorted(knowledge.known_source_terms)[:15]:
            glossary_rows.append(term)

    asr_rows = [
        f"{key} → {value}"
        for key, value in sorted(
            knowledge.asr_corrections.items(), key=lambda kv: -len(kv[0]),
        )[:20]
    ]
    before_section = _format_section([
        f"[{i}] {text}" for i, text in enumerate(before, start=1)
    ])
    after_section = _format_section([
        f"[{i}] {text}" for i, text in enumerate(after, start=1)
    ])

    return template.format(
        lang=_language_label(knowledge.source_language),
        glossary_section=_format_section(glossary_rows),
        asr_section=_format_section(asr_rows),
        before_section=before_section,
        after_section=after_section,
        primary=unit.primary_evidence or "（无）",
        secondary=unit.secondary_evidence or "（无）",
    )


def _parse_reconstruction(response: str, source_language: str = "ko") -> str:
    """解析重建响应：只取源语言句子；unresolved/拒绝 → 空。

    支持韩语（Hangul）和日语（假名）。
    """
    text = (response or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low.startswith("unresolved") or low.startswith("无法"):
        return ""
    # 去掉常见包装（引号、解释性前缀行）
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    candidate = lines[0].strip(" \t\"'“”「」『』")
    # 重建结果必须含源语言文字（韩文音节或日文假名）
    if not _SOURCE_SCRIPT_RE.search(candidate):
        return ""
    if len(candidate) > 200:
        candidate = candidate[:200]
    return candidate


def _response_candidate(response: str) -> str:
    """取模型首个非空行，供 acceptance gate 观察被 parser 拒绝的输出。"""
    lines = [line.strip() for line in (response or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[0].strip(" \t\"'“”「」『』")[:200]


def _marker_set(text: str, markers) -> set[str]:
    return {marker for marker in markers if marker in (text or "")}


def _surface_tokens(text: str, source_language: str) -> set[str]:
    if source_language == "ko":
        return set(re.findall(r"[\uac00-\ud7af]+", text or ""))
    if source_language == "ja":
        return set(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text or ""))
    return set(re.findall(r"[A-Za-z0-9']+", (text or "").lower()))


def _question_state(text: str, source_language: str) -> bool:
    if "?" in text or "？" in text:
        return True
    compact = re.sub(r"[\s.!。！]+$", "", text or "")
    if source_language == "ko":
        return bool(re.search(r"(?:까|나요|니|냐|습니까)$", compact))
    return compact.endswith(("か", "の", "かな", "でしょうか"))


def _evaluate_reconstruction_acceptance(
    unit: ArbitratedUnit,
    rebuilt: str,
    knowledge: ArbitrationKnowledge,
    before: List[str],
    after: List[str],
) -> tuple[str, List[str]]:
    """纯规则三态门禁；上下文只用于识别风险，绝不作为新增信息依据。"""
    proposed = (rebuilt or "").strip()
    evidence = [
        text.strip() for text in (
            unit.canonical_source_text,
            unit.primary_evidence,
            unit.secondary_evidence,
        ) if text and text.strip()
    ]
    if not proposed:
        return "unresolved", ["empty_reconstruction"]

    if unit.source_language == "ko" and _HAN_RE.search(proposed):
        return "rejected", ["target_language_content"]
    required_script = _HANGUL_RE if unit.source_language == "ko" else _KANA_RE
    if not required_script.search(proposed):
        return "rejected", ["source_language_mismatch"]

    evidence_numbers = [_NUMBER_RE.findall(text) for text in evidence]
    proposed_numbers = _NUMBER_RE.findall(proposed)
    if evidence_numbers and not any(numbers == proposed_numbers for numbers in evidence_numbers):
        return "rejected", ["number_changed"]

    negation_markers = _NEGATION_MARKERS.get(unit.source_language, ())
    evidence_negations = [_marker_set(text, negation_markers) for text in evidence]
    proposed_negations = _marker_set(proposed, negation_markers)
    if evidence_negations and not any(
        markers == proposed_negations for markers in evidence_negations
    ):
        return "rejected", ["negation_changed"]

    evidence_questions = {
        _question_state(text, unit.source_language) for text in evidence
    }
    if evidence_questions and _question_state(
        proposed, unit.source_language,
    ) not in evidence_questions:
        return "rejected", ["question_changed"]

    compact_proposed = _normalise_for_compare(proposed, unit.source_language)
    compact_evidence = [
        _normalise_for_compare(text, unit.source_language) for text in evidence
    ]
    similarities = [
        SequenceMatcher(None, compact_proposed, compact).ratio()
        for compact in compact_evidence if compact
    ]
    max_similarity = max(similarities, default=0.0)
    longest_evidence = max((len(text) for text in compact_evidence), default=0)
    proposed_tokens = _surface_tokens(proposed, unit.source_language)
    primary_tokens = _surface_tokens(
        unit.primary_evidence, unit.source_language,
    )
    secondary_tokens = _surface_tokens(
        unit.secondary_evidence, unit.source_language,
    )
    union_supported = bool(
        proposed_tokens
        and proposed_tokens <= (primary_tokens | secondary_tokens)
    )
    if (
        longest_evidence
        and len(compact_proposed) > longest_evidence * 1.6
        and not union_supported
    ):
        return "rejected", ["large_unsupported_addition"]
    if max_similarity < 0.35:
        return "rejected", ["low_evidence_similarity"]

    # 已知 ASR correction 是强确定性证据，应先于“新增实体”启发式判断。
    for raw in evidence:
        normalised = knowledge.normalize_via_corrections(raw)
        if normalised != raw and _normalise_for_compare(
            normalised, unit.source_language,
        ) == compact_proposed:
            return "accepted", ["asr_correction"]

    relation_markers = _RELATIONSHIP_MARKERS.get(unit.source_language, ())
    evidence_relations = set().union(*(
        _marker_set(text, relation_markers) for text in evidence
    )) if evidence else set()
    proposed_relations = _marker_set(proposed, relation_markers)
    if proposed_relations - evidence_relations:
        return "rejected", ["new_relationship"]

    evidence_text = " ".join(evidence)
    context_text = " ".join(before + after)
    evidence_unknowns = set(_extract_entity_candidates(evidence_text, knowledge))
    context_unknowns = set(_extract_entity_candidates(context_text, knowledge))
    proposed_unknowns = set(_extract_entity_candidates(proposed, knowledge))
    proposed_known = {
        term for term in knowledge.known_source_terms if term in proposed
    }
    evidence_known = {
        term for term in knowledge.known_source_terms if term in evidence_text
    }
    new_known = proposed_known - evidence_known
    if (
        len(new_known) == 1
        and max_similarity >= 0.75
        and any(
            SequenceMatcher(
                None,
                _normalise_for_compare(known, unit.source_language),
                _normalise_for_compare(unknown, unit.source_language),
            ).ratio() >= 0.5
            for known in new_known
            for unknown in evidence_unknowns
        )
    ):
        return "accepted", ["glossary_entity_repair"]
    novel_unknowns = proposed_unknowns - evidence_unknowns
    if novel_unknowns:
        reason = (
            "context_only_entity"
            if novel_unknowns <= context_unknowns else "new_entity"
        )
        return "rejected", [reason]
    new_people = {
        term for term in knowledge.person_terms
        if term in proposed and term not in evidence_text
    }
    if new_people:
        return "rejected", ["new_entity"]

    # 1) 直接采用一条完整 source，或用完整 source 替换 canonical 残片。
    for raw in (unit.primary_evidence, unit.secondary_evidence):
        if not raw:
            continue
        if _normalise_for_compare(raw, unit.source_language) == compact_proposed:
            if _is_fragment(unit.canonical_source_text, knowledge) and not _is_fragment(
                raw, knowledge,
            ):
                return "accepted", ["full_source_replaces_fragment"]
            return "accepted", ["source_evidence_exact"]

    # 2) 重建内容全部来自两路 token 的并集，且至少与一路有中等相似度。
    if (
        union_supported
        and primary_tokens
        and secondary_tokens
        and proposed_tokens & (primary_tokens - secondary_tokens)
        and proposed_tokens & (secondary_tokens - primary_tokens)
        and max_similarity >= 0.5
    ):
        return "accepted", ["dual_source_supported_completion"]

    return "unresolved", ["insufficient_deterministic_support"]


def reconstruct_sources(
    units: Dict[str, ArbitratedUnit],
    candidates: List,
    *,
    source_language: str,
    data_dir: Optional[str] = None,
    translator=None,
    max_units: int = 25,
    progress_callback=None,
    trace_path: Optional[str] = None,
) -> Dict[str, Dict]:
    """对高风险句调用 LLM 重建源文本。

    translator：OpenAICompatibleTranslator 实例（复用流式/重试/reasoning_effort=low）。
    trace_path：如果提供，将完整 trace 落盘到该路径（<union>.reconstruction.json）。
    返回 {key: {"text": ..., "decision": ...}}；skipped 表示未触发。
    """
    results: Dict[str, Dict] = {}
    trace_entries: List[Dict] = []
    if source_language == "en" or not translator:
        # 全部记为 not_eligible（en 不重建）
        for unit in units.values():
            trace_entries.append(_trace_entry(
                unit, eligible=False, eligibility_reasons=[],
                status="not_eligible", applied=False,
            ))
        _save_trace(trace_path, trace_entries, source_language)
        return results

    knowledge = ArbitrationKnowledge.from_data_dir(
        data_dir, game="wuwa", source_language=source_language,
    )
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    ordered = [unit for unit in units.values() if should_reconstruct(unit)]
    ordered.sort(key=lambda unit: (unit.start_ms, unit.end_ms))

    # 记录 eligible 但未调用（超过 cap 的）
    capped = ordered[max_units:]
    ordered = ordered[:max_units]

    # 前后文：按时间序取整个 ordered 列表的相邻句（用全部 canonical/evidence）
    all_keys = sorted(units.keys())
    for unit in ordered:
        try:
            before, after = _neighbour_texts(unit, units, all_keys)
        except Exception:
            before, after = [], []
        prompt = build_reconstruction_prompt(unit, before, after, knowledge)
        raw_response = ""
        error_msg = ""
        try:
            # max_tokens=512：推理模型 reasoning 可能占 200+ token；
            # 256 太小导致 content 为空概率高。
            response, _, _ = translator._request_api(
                prompt, max_tokens=512, attempts=3,
            )
            raw_response = response
        except Exception as exc:
            error_msg = type(exc).__name__
            results[unit.key] = {
                "text": "",
                "decision": "error",
                "error": error_msg,
                "acceptance": "unresolved",
                "acceptance_reasons": ["reconstruction_error"],
            }
            trace_entries.append(_trace_entry(
                unit, eligible=True, eligibility_reasons=_eligibility_reasons(unit),
                status="called_error", applied=False,
                reconstruction_error=error_msg,
                acceptance="unresolved",
                acceptance_reasons=["reconstruction_error"],
            ))
            if progress_callback:
                try:
                    progress_callback(1)
                except Exception:
                    pass
            continue
        rebuilt = _parse_reconstruction(raw_response, source_language)
        proposed = rebuilt or _response_candidate(raw_response)
        is_explicit_unresolved = (raw_response or "").strip().lower().startswith(
            ("unresolved", "无法")
        )
        if proposed and not is_explicit_unresolved:
            acceptance, acceptance_reasons = _evaluate_reconstruction_acceptance(
                unit, proposed, knowledge, before, after,
            )
            accepted = acceptance == "accepted"
            results[unit.key] = {
                "text": proposed if accepted else "",
                "decision": "reconstructed" if accepted else acceptance,
                "acceptance": acceptance,
                "acceptance_reasons": acceptance_reasons,
            }
            trace_entries.append(_trace_entry(
                unit, eligible=True, eligibility_reasons=_eligibility_reasons(unit),
                status="called_resolved", applied=False,
                reconstructed_text=proposed,
                canonical_before=unit.canonical_source_text,
                canonical_after=(
                    proposed if accepted else unit.canonical_source_text
                ),
                acceptance=acceptance,
                acceptance_reasons=acceptance_reasons,
            ))
        else:
            results[unit.key] = {
                "text": "",
                "decision": "unresolved",
                "acceptance": "unresolved",
                "acceptance_reasons": ["model_unresolved"],
            }
            trace_entries.append(_trace_entry(
                unit, eligible=True, eligibility_reasons=_eligibility_reasons(unit),
                status="called_unresolved", applied=False,
                raw_response_preview=raw_response[:200] if raw_response else "",
                acceptance="unresolved",
                acceptance_reasons=["model_unresolved"],
            ))
        if progress_callback:
            try:
                progress_callback(1)
            except Exception:
                pass

    # 记录 eligible_not_called（超过 cap 的）
    for unit in capped:
        trace_entries.append(_trace_entry(
            unit, eligible=True, eligibility_reasons=_eligibility_reasons(unit),
            status="eligible_not_called", applied=False,
        ))

    # 记录 not_eligible
    eligible_keys = {u.key for u in ordered} | {u.key for u in capped}
    for unit in units.values():
        if unit.key not in eligible_keys:
            trace_entries.append(_trace_entry(
                unit, eligible=False, eligibility_reasons=[],
                status="not_eligible", applied=False,
            ))

    _save_trace(trace_path, trace_entries, source_language)
    return results


def _eligibility_reasons(unit: ArbitratedUnit) -> List[str]:
    """返回触发重建的原因列表。"""
    reasons = []
    if unit.source_decision == "unresolved":
        reasons.append("source_unresolved")
    if unit.source_conflict and unit.source_confidence < 0.45:
        reasons.append("low_confidence_conflict")
    if unit.unknown_entities and unit.source_conflict:
        reasons.append("unknown_entity_conflict")
    return reasons


def _trace_entry(
    unit: ArbitratedUnit,
    *, eligible: bool,
    eligibility_reasons: List[str],
    status: str,
    applied: bool,
    reconstructed_text: str = "",
    canonical_before: str = "",
    canonical_after: str = "",
    reconstruction_error: str = "",
    raw_response_preview: str = "",
    acceptance: str = "unresolved",
    acceptance_reasons: Optional[List[str]] = None,
) -> Dict:
    """构建一条 trace 记录。不记录 API Key / reasoning_content。"""
    entry = {
        "unit_id": unit.key,
        "start_ms": unit.start_ms,
        "end_ms": unit.end_ms,
        "eligible": eligible,
        "eligibility_reasons": eligibility_reasons,
        "primary_text": unit.primary_evidence,
        "secondary_text": unit.secondary_evidence,
        "canonical_before": canonical_before or unit.canonical_source_text,
        "reconstruction_called": status.startswith("called_"),
        "reconstruction_status": status,
        "reconstructed_text": reconstructed_text,
        "canonical_after": canonical_after,
        "applied": applied,
        "acceptance": acceptance,
        "acceptance_reasons": list(acceptance_reasons or []),
    }
    if reconstruction_error:
        entry["reconstruction_error"] = reconstruction_error
    if raw_response_preview:
        entry["raw_response_preview"] = raw_response_preview
    return entry


def _save_trace(
    trace_path: Optional[str],
    entries: List[Dict],
    source_language: str,
) -> None:
    """将 trace 落盘为 JSON 文件。"""
    if not trace_path:
        return
    try:
        summary = {
            "source_language": source_language,
            "total_units": len(entries),
            "eligible": sum(1 for e in entries if e["eligible"]),
            "called": sum(1 for e in entries if e["reconstruction_called"]),
            "resolved": sum(1 for e in entries if e["reconstruction_status"] == "called_resolved"),
            "unresolved": sum(1 for e in entries if e["reconstruction_status"] == "called_unresolved"),
            "error": sum(1 for e in entries if e["reconstruction_status"] == "called_error"),
            "applied": sum(1 for e in entries if e["applied"]),
            "accepted": sum(1 for e in entries if e.get("acceptance") == "accepted"),
            "rejected": sum(1 for e in entries if e.get("acceptance") == "rejected"),
            "acceptance_unresolved": sum(
                1 for e in entries if e.get("acceptance") == "unresolved"
            ),
        }
        # 与 entry 的 accepted/rejected 命名并存；accept/reject 是面向
        # summary 消费者的简短计数键，旧 unresolved 语义保持不变。
        summary["accept"] = summary["accepted"]
        summary["reject"] = summary["rejected"]
        payload = {"summary": summary, "entries": entries}
        target = os.path.dirname(trace_path)
        if target:
            os.makedirs(target, exist_ok=True)
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", dir=target or ".",
            delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp_path = tmp.name
        os.replace(tmp_path, trace_path)
        log.info(
            "Reconstruction trace saved: %s (eligible=%d, called=%d, resolved=%d, applied=%d)",
            Path(trace_path).name, summary["eligible"], summary["called"],
            summary["resolved"], summary["applied"],
        )
    except Exception:
        log.warning("Failed to save reconstruction trace; details discarded")


def update_trace_applied(
    trace_path: Optional[str],
    applied_keys: set[str],
) -> None:
    """重建结果写回 union 后，更新 trace 中的 applied 字段。"""
    if not trace_path or not os.path.isfile(trace_path):
        return
    try:
        with open(trace_path, encoding="utf-8-sig") as f:
            payload = json.loads(f.read())
        for entry in payload.get("entries", []):
            if (
                entry.get("unit_id") in applied_keys
                and entry.get("acceptance") in {None, "accepted"}
            ):
                entry["applied"] = True
                if entry.get("reconstruction_status") == "called_resolved":
                    entry["canonical_after"] = entry.get("reconstructed_text", "")
        payload["summary"]["applied"] = sum(
            1 for e in payload.get("entries", []) if e.get("applied")
        )
        import tempfile
        target = os.path.dirname(trace_path)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", dir=target or ".",
            delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp_path = tmp.name
        os.replace(tmp_path, trace_path)
    except Exception:
        log.warning("Failed to update reconstruction trace applied; details discarded")


def _neighbour_texts(unit, units, ordered_keys) -> (List[str], List[str]):
    """按时间序取 unit 前后各 2 句（canonical 优先）。"""
    idx = ordered_keys.index(unit.key) if unit.key in ordered_keys else -1
    before, after = [], []
    if idx >= 0:
        for key in ordered_keys[max(0, idx - 2):idx]:
            neighbour = units[key]
            text = neighbour.canonical_source_text or neighbour.primary_evidence
            if text:
                before.append(text)
        for key in ordered_keys[idx + 1:idx + 3]:
            neighbour = units[key]
            text = neighbour.canonical_source_text or neighbour.primary_evidence
            if text:
                after.append(text)
    return before, after
