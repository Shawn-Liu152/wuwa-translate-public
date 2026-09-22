# -*- coding: utf-8 -*-
"""Benchmark 运行器（提示词阶段 G）。

用法：
  python tools/run_benchmark.py                      # source 层（离线）
  python tools/run_benchmark.py --translate --limit 30   # + 翻译层（真实 LLM）

指标（不依赖 BLEU 作为主指标）：
- Source 层：canonical 决策分布 / unresolved recall / 术语保留率
- Translation 层（需 gold）：实体准确率 / 术语准确率 / 数字保留 / 否定保留 /
  韩文残留 / 幻觉实体数
- 输出 JSON 报告到 benchmarks/report-<时间戳>.json
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline.config import Config, infer_provider, normalize_llm_base_url


PROVIDER_BASE_URLS = {
    "opencode-go": "https://opencode.ai/zen/go/v1",
    "deepseek": "https://api.deepseek.com/v1",
}

NEGATION_WORDS = ("不", "没", "别", "无", "非", "未")
# 金标中的实体目标名
ENTITY_TARGETS = ("爱弥斯", "绯雪", "达妮娅", "隧门", "隧者", "虚质磁暴",
                  "残星会", "罗伊", "拉海洛")


def configure_utf8_stdio():
    """Keep multilingual CLI JSON lossless on Windows console/pipe code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")


def resolve_provider_settings(provider=None, base_url=None):
    """Resolve benchmark backend settings through the shared pipeline config."""
    requested_provider = str(provider or "").strip()
    if base_url is not None:
        requested_url = normalize_llm_base_url(base_url, required=False)
        return requested_provider or infer_provider(requested_url), requested_url
    if not requested_provider:
        configured_url = normalize_llm_base_url(
            Config.LLM_BASE_URL, required=False
        )
        return Config.PROVIDER, configured_url
    if requested_provider not in PROVIDER_BASE_URLS:
        raise ValueError(f"unknown provider: {requested_provider}")
    return requested_provider, PROVIDER_BASE_URLS[requested_provider]


def load_benchmark(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_ids_file(path):
    """Load a JSON ID list/object or one ID per non-comment text line."""
    source = Path(path)
    raw = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".json":
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload.get("ids")
        if not isinstance(payload, list):
            raise ValueError("ids file JSON must be a list or an object with ids")
        return [str(value) for value in payload]
    return [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def select_entries(
    benchmark, *, language=None, gold_status=None, ids=None,
    limit=None, seed=0,
):
    """Select a stable benchmark sample without depending on file order."""
    benchmark_language = str(benchmark.get("source_language") or "ko")
    if language and language != benchmark_language:
        raise ValueError(
            f"benchmark language is {benchmark_language}, not {language}"
        )
    entries = [
        entry for entry in (benchmark.get("entries") or [])
        if isinstance(entry, dict)
        and (
            not entry.get("source_language")
            or entry.get("source_language") == benchmark_language
        )
    ]
    if gold_status and gold_status != "all":
        entries = [
            entry for entry in entries
            if entry.get("gold_status") == gold_status
        ]
    if ids is not None:
        requested = [str(value) for value in ids]
        if len(requested) != len(set(requested)):
            raise ValueError("duplicate benchmark ID in ids file")
        by_id = {str(entry.get("id")): entry for entry in entries}
        missing = [value for value in requested if value not in by_id]
        if missing:
            raise ValueError(f"unknown benchmark ID: {missing[0]}")
        entries = [by_id[value] for value in requested]
    else:
        entries = sorted(
            entries,
            key=lambda entry: hashlib.sha256(
                f"{seed}:{entry.get('id')}".encode("utf-8")
            ).hexdigest(),
        )
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        entries = entries[:limit]
    return entries


def _file_sha256(path):
    source = Path(path)
    if not source.is_file():
        return None
    return hashlib.sha256(source.read_bytes()).hexdigest()


def artifact_hashes(*, prompt, glossary, translation_memory, benchmark):
    """Fingerprint every mutable input that can affect a benchmark run."""
    return {
        "prompt_sha256": _file_sha256(prompt),
        "glossary_sha256": _file_sha256(glossary),
        "tm_sha256": _file_sha256(translation_memory),
        "benchmark_sha256": _file_sha256(benchmark),
    }


def audit_tm_leakage(entries, translation_memory, *, language):
    """List normalized exact overlap between holdout sources and approved TM."""
    from pipeline.translation_memory import normalize_source

    approved = [
        item for item in (translation_memory.get("entries") or [])
        if isinstance(item, dict)
        and item.get("approved") is True
        and item.get("source_language") == language
    ]
    memory_by_source = {}
    for item in approved:
        normalized = normalize_source(
            item.get("source", ""), source_language=language,
        )
        if normalized:
            memory_by_source.setdefault(normalized, []).append(item)
    matches = []
    for entry in entries:
        source = (
            entry.get("canonical_source_text")
            or entry.get("source")
            or entry.get("original")
            or ""
        )
        normalized = normalize_source(source, source_language=language)
        for item in memory_by_source.get(normalized, []):
            matches.append({
                "benchmark_id": str(entry.get("id")),
                "tm_id": str(item.get("id")),
                "normalized_source": normalized,
            })
    return {
        "checked_entries": len(entries),
        "approved_tm_entries": len(approved),
        "match_count": len(matches),
        "matches": matches,
    }


def require_api_key(value):
    """Validate the credential gate without ever including the value in errors."""
    key = str(value or "").strip()
    if not key:
        raise ValueError("LLM_API_KEY is required for --translate")
    return key


def translate_with_pipeline(entries, *, model, api_key, base_url, proxy,
                            source_language="ko"):
    """用真实翻译链路跑 benchmark 条目（单条构建 prompt，flash 直译）。

    Phase 10 (2026-08-11): source_language 参数化——EN/JA/KO 三语
    共用同一链路，按语言加载对应 prompt 模板与术语知识。
    """
    from pipeline.preprocess.glossary import load_glossary_db
    from pipeline.preprocess.source_arbitration import ArbitrationKnowledge
    from pipeline.translate.prompt_builder import PromptBuilder
    from pipeline.translate.llm import OpenAICompatibleTranslator
    from pipeline.parser.srt_parser import Subtitle

    knowledge = ArbitrationKnowledge.from_data_dir(
        os.path.join(BASE, "data"), game="wuwa",
        source_language=source_language,
    )
    database = load_glossary_db(os.path.join(BASE, "data"))
    builder = PromptBuilder(
        os.path.join(BASE, "data", "prompts", f"{source_language}-zh-CN.txt"),
        source_language=source_language,
    )
    translator = OpenAICompatibleTranslator(
        api_key=api_key, model=model, base_url=base_url, proxy=proxy,
        temperature=0.1,
    )
    results = []
    tokens_in_total = 0
    tokens_out_total = 0
    error_count = 0
    started_at = time.perf_counter()
    for entry in entries:
        subs = [Subtitle(1, "00:00:00,000", "00:00:02,000",
                         entry["canonical_source_text"])]
        # 与真实流水线一致：按 canonical 命中注入术语
        glossary = database.extract_from_text(
            entry["canonical_source_text"], game="wuwa",
            source_language=source_language,
        )
        prompt = builder.build(subs, glossary=glossary,
                               translation_memory=[],
                               global_context={"game": "wuwa"})
        try:
            response, tokens_in, tokens_out = translator._request_api(
                prompt, max_tokens=256, attempts=2,
            )
            tokens_in_total += int(tokens_in or 0)
            tokens_out_total += int(tokens_out or 0)
            translated = _extract_translation(response)
        except Exception as exc:
            error_count += 1
            translated = f"__ERROR__:{type(exc).__name__}"
        results.append({"entry_id": entry["id"],
                        "translation": translated})
    translator.close()
    usage = {
        "tokens_in": tokens_in_total,
        "tokens_out": tokens_out_total,
        "tokens_total": tokens_in_total + tokens_out_total,
        "wall_seconds": round(time.perf_counter() - started_at, 3),
        "error_count": error_count,
        "cost": None,
        "cost_availability": "unavailable",
    }
    return results, usage


def _extract_translation(response: str) -> str:
    lines = [l.strip() for l in (response or "").splitlines()
             if l.strip() and ("::" in l or re.match(r"^\[\d+\]", l))]
    for line in lines:
        if "::" in line:
            parts = line.split("::", 1)
            if parts[0].strip().isdigit():
                return parts[1].strip()
        m = re.match(r"^\[\d+\]\s*(.*)$", line)
        if m:
            return m.group(1).strip()
    return (response or "").strip()[:120]


def source_metrics(benchmark):
    entries = benchmark.get("entries") or []
    tags = {}
    for e in entries:
        for t in (e.get("error_tags") or []):
            tags[t] = tags.get(t, 0) + 1
    unresolved = sum(
        1 for e in entries
        if any(
            "unresolved" in str(tag).casefold()
            for tag in (e.get("error_tags") or [])
        )
    )
    entity_terms = {}
    for e in entries:
        if e.get("entities"):
            src = e["entities"][0]["source"]
            entity_terms[src] = entity_terms.get(src, 0) + 1
    return {
        "total": len(entries),
        "gold_count": sum(1 for e in entries if e.get("gold_zh")),
        "error_tag_distribution": tags,
        "unresolved_recall": (
            round(unresolved / len(entries), 3) if entries else None
        ),
        "entity_mentions": entity_terms,
        "entity_rows": sum(1 for e in entries if e.get("entities")),
    }


def translation_metrics(benchmark, translations, reference_translations=None):
    """翻译层指标：gold 对（30 条）+ 全量启发式。"""
    by_id = {t["entry_id"]: t["translation"] for t in translations}
    gold_entries = [e for e in benchmark["entries"] if e["gold_zh"]]
    entity_acc = 0
    entity_total = 0
    gold_exact = 0
    hangul_residue = 0
    hallucinated = 0
    checked = 0
    number_checked = 0
    number_correct = 0
    negation_correct = 0
    question_correct = 0
    for e in gold_entries:
        text = by_id.get(e["id"], "")
        if not text or text.startswith("__ERROR__"):
            continue
        checked += 1
        gold_text = str(e.get("gold_zh") or "")
        if e["gold_zh"] and text.strip() == e["gold_zh"].strip():
            gold_exact += 1
        gold_numbers = re.findall(r"\d+(?:\.\d+)?", gold_text)
        if gold_numbers:
            number_checked += 1
            predicted_numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if sorted(predicted_numbers) == sorted(gold_numbers):
                number_correct += 1
        gold_has_negation = any(word in gold_text for word in NEGATION_WORDS)
        predicted_has_negation = any(word in text for word in NEGATION_WORDS)
        if gold_has_negation == predicted_has_negation:
            negation_correct += 1
        if bool(re.search(r"[?？]", gold_text)) == bool(
            re.search(r"[?？]", text)
        ):
            question_correct += 1
        if e["entities"]:
            entity_total += len(e["entities"])
            entity_acc += sum(
                1 for ent in e["entities"]
                if ent["target"] in text
            )
        if re.search(r"[\uac00-\ud7af]", text):
            hangul_residue += 1
        if re.search(r"[\u4e00-\u9fff]{2,4}", text):
            for word in re.findall(r"[\u4e00-\u9fff]{2,4}", text):
                if word not in ENTITY_TARGETS and word not in (
                        "这个", "那个", "什么", "怎么", "为什么", "就是",
                        "不是", "没有", "不要", "真的", "现在", "然后",
                        "不过", "但是", "所以", "因为", "如果", "已经",
                        "可以", "应该", "可能", "觉得", "知道", "看到",
                        "非常", "特别", "完全", "终于", "突然", "这样",
                        "那样", "一下", "一起", "一直", "一定", "一个",
                        "特么", "真特么", "完蛋", "剑鞘", "拔出来", "岔开",
                        "太帅", "好帅", "帅爆", "离谱", "夸张", "疯狂",
                        "疯了", "搞笑", "无语", "绝了", "厉害", "牛批",
                        "我靠", "卧槽", "我的天", "天哪", "哎呀", "哇塞",
                        "妈呀", "救命", "要命", "什么鬼", "咋回事"):
                    hallucinated += 1
                    break
    return {
        "checked_gold": checked,
        "gold_exact_match": gold_exact,
        "entity_accuracy": round(entity_acc / entity_total, 3)
        if entity_total else None,
        "number_checked": number_checked,
        "number_accuracy": round(number_correct / number_checked, 3)
        if number_checked else None,
        "negation_accuracy": round(negation_correct / checked, 3)
        if checked else None,
        "question_accuracy": round(question_correct / checked, 3)
        if checked else None,
        "hangul_residue_units": hangul_residue,
        "suspected_hallucinated_units": hallucinated,
    }


def entity_metrics(benchmark):
    """ENTITY 层（Phase 10）：实体命中/变体/错误类型分布。"""
    entries = benchmark["entries"]
    entity_total = 0
    entity_hit = 0
    variant_count = 0
    bad_entity_units = 0
    for e in entries:
        ents = e.get("entities") or []
        if not ents:
            continue
        entity_total += len(ents)
        # 期望目标名（gold 或 entities[].target）
        gold_text = e.get("gold_zh") or ""
        for ent in ents:
            target = ent.get("target") or ""
            if not target:
                continue
            if gold_text and target in gold_text:
                entity_hit += 1
            if ent.get("variant"):
                variant_count += 1
        if any(t in (e.get("error_tags") or []) for t in ("ENTITY_BAD",)):
            bad_entity_units += 1
    return {
        "entity_rows": sum(1 for e in entries if e.get("entities")),
        "entity_mentions": entity_total,
        "gold_entity_hits": entity_hit,
        "asr_variant_entities": variant_count,
        "entity_bad_units": bad_entity_units,
    }


def display_metrics(benchmark):
    """DISPLAY 层（Phase 10）：时间轴/残片/重叠等确定性检查。"""
    entries = benchmark["entries"]
    overlap = 0
    nonpositive = 0
    residual = 0
    long_gap = 0
    prev_end = None
    for e in sorted(entries, key=lambda x: x.get("start_ms", 0)):
        start = e.get("start_ms", 0)
        end = e.get("end_ms", 0)
        if end <= start:
            nonpositive += 1
        if prev_end is not None and start < prev_end:
            overlap += 1
        if prev_end is not None and start - prev_end > 5000:
            long_gap += 1
        prev_end = max(prev_end or 0, end)
    return {
        "units": len(entries),
        "overlap": overlap,
        "nonpositive_duration": nonpositive,
        "gap_over_5s": long_gap,
        "note": (
            "benchmark 条目是抽样快照（非连续时间轴），overlap/gap "
            "是抽样伪信号；完整字幕的 display 验证须对 final.srt 全量跑"
        ),
    }


def main():
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run benchmark")
    parser.add_argument("--benchmark")
    parser.add_argument("--language", choices=("en", "ja", "ko"))
    parser.add_argument(
        "--gold-status", choices=("confirmed", "provisional", "all"),
        default="all",
    )
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--translate", action="store_true",
                        help="跑真实翻译层（需 API）")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--seed", type=int, default=0,
        help="selection ordering only; model sampling is not seeded",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=Config.LLM_MODEL)
    parser.add_argument("--provider", default=None,
                        help="provider name (default: shared Config.PROVIDER)")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    try:
        provider, base_url = resolve_provider_settings(
            args.provider, args.base_url,
        )
    except ValueError as error:
        parser.error(str(error))

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    requested_language = args.language or "ko"
    benchmark_path = Path(args.benchmark) if args.benchmark else Path(
        BASE, "benchmarks", f"{requested_language}_reaction_benchmark.json"
    )
    benchmark = load_benchmark(benchmark_path)
    source_language = benchmark.get("source_language", "ko")
    try:
        ids = load_ids_file(args.ids_file) if args.ids_file else None
        selected = select_entries(
            benchmark,
            language=args.language,
            gold_status=args.gold_status,
            ids=ids,
            limit=args.limit,
            seed=args.seed,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    selected_benchmark = dict(benchmark)
    selected_benchmark["entries"] = selected
    prompt_path = Path(BASE, "data", "prompts", f"{source_language}-zh-CN.txt")
    glossary_path = Path(BASE, "data", "glossary.db")
    tm_path = Path(BASE, "data", "translation_memory.json")
    try:
        translation_memory = json.loads(tm_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        translation_memory = {"entries": []}
    # Phase 10 (2026-08-11): 分层报告——SOURCE/TRANSLATION/ENTITY/
    # DISPLAY，不合并成单一总分（任务书 #56）。
    report = {
        "name": benchmark["name"],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": Config.provider_fingerprint(base_url, provider),
        "model": args.model,
        "source_language": source_language,
        "selected_ids": [str(entry.get("id")) for entry in selected],
        "selection": {
            "gold_status": args.gold_status,
            "ids_file": str(args.ids_file.resolve()) if args.ids_file else None,
            "limit": args.limit,
            "repeat": args.repeat,
            "seed": args.seed,
            "seed_scope": "selection_order_only",
        },
        "hashes": artifact_hashes(
            prompt=prompt_path,
            glossary=glossary_path,
            translation_memory=tm_path,
            benchmark=benchmark_path,
        ),
        "tm_leakage": audit_tm_leakage(
            selected, translation_memory, language=source_language,
        ),
        "layers": {
            "SOURCE": source_metrics(selected_benchmark),
            "ENTITY": entity_metrics(selected_benchmark),
            "DISPLAY": display_metrics(selected_benchmark),
        },
    }
    if args.translate:
        try:
            api_key = require_api_key(Config.LLM_API_KEY)
        except ValueError as error:
            parser.error(str(error))
        runs = []
        first_translations = []
        for repeat_index in range(args.repeat):
            translations, usage = translate_with_pipeline(
                selected, model=args.model, api_key=api_key,
                base_url=base_url, proxy=args.proxy,
                source_language=source_language,
            )
            if repeat_index == 0:
                first_translations = translations
            runs.append({
                "repeat_index": repeat_index,
                "metrics": translation_metrics(
                    selected_benchmark, translations,
                ),
                "usage": usage,
            })
        report["layers"]["TRANSLATION"] = {
            "repeat": args.repeat,
            "runs": runs,
        }
        report["sample_translations"] = first_translations[:10]

    out_path = args.output or Path(
        BASE, "benchmarks", f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告: {out_path}")


if __name__ == "__main__":
    main()
