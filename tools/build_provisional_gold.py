#!/usr/bin/env python3
"""从 rerun 三语 round2 results 提取 provisional gold（幂等）。

原则（任务书 #55）：Reviewer keep + 高置信 ≠ 人工确认 gold。
提取结果标记 gold_status="provisional"（待人工确认），
绝不冒充 confirmed；confirmed gold 必须人工审核。

输入：
  download/rerun-{lang}-20260810/final.zh.round2.results.json
  download/rerun-{lang}-20260810/final.zh.risk.generated.json
输出：
  benchmarks/{lang}_reaction_benchmark.json 追加 provisional entries
  （仅当 entry 尚不存在——按 id 去重）
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGUAGES = ["en", "ja", "ko"]
MIN_CONFIDENCE = 0.9


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_provisional(lang: str, dry_run: bool = True) -> list[dict]:
    job = f"rerun-{lang}-20260810"
    r2_path = os.path.join(BASE, "download", job, "final.zh.round2.results.json")
    risk_path = os.path.join(BASE, "download", job, "final.zh.risk.generated.json")
    bench_path = os.path.join(BASE, "benchmarks", f"{lang}_reaction_benchmark.json")
    if not os.path.isfile(r2_path):
        print(f"[{lang}] no round2 results: {r2_path}")
        return []

    r2 = load(r2_path)
    risk = load(risk_path)
    bench = load(bench_path)
    risk_map = {i.get("key"): i for i in risk["items"]}
    existing = {str(e["id"]) for e in bench["entries"]}
    # 只取显式 keep 决策（非 unresolved/deferred），confidence >= 0.9
    candidates = [
        i for i in r2["items"]
        if i.get("decision") == "keep"
        and (i.get("confidence") or 0) >= MIN_CONFIDENCE
    ]
    added = 0
    for i in candidates:
        key = i.get("key")
        ritem = risk_map.get(key)
        if not ritem:
            continue
        source = (ritem.get("source_text") or "").strip()
        translated = (i.get("round2_text") or i.get("translated") or "").strip()
        if not source or not translated:
            continue
        # 源文本须是完整自然句（≥3 个非空白字符、不以残片标点结尾）
        if len(source) < 3 or source.endswith(("…", "…\n", "-")):
            continue
        eid = str(ritem.get("key") or key)
        if eid in existing:
            continue
        entry = {
            "id": eid,
            "start_ms": ritem.get("start_ms", 0),
            "end_ms": ritem.get("end_ms", 0),
            "youtube_text": ritem.get("primary_evidence") or "",
            "whisper_text": ritem.get("secondary_evidence") or "",
            "canonical_source_text": source,
            "gold_zh": translated,
            "gold_status": "provisional",
            "entities": ritem.get("entities") or [],
            "error_tags": ritem.get("reasons") or [],
        }
        if not dry_run:
            bench["entries"].append(entry)
        added += 1

    if not dry_run:
        with open(bench_path, "w", encoding="utf-8") as f:
            json.dump(bench, f, ensure_ascii=False, indent=2)
    print(f"[{lang}] provisional candidates={len(candidates)} added={added} "
          f"(dry_run={dry_run})")
    return [c for c in candidates[:0]]  # 保持简洁


if __name__ == "__main__":
    dry = "--write" not in sys.argv
    for lang in LANGUAGES:
        build_provisional(lang, dry_run=dry)
    print("dry-run 完成；确认无误后加 --write 写入")
