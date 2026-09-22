#!/usr/bin/env python3
"""从历史审计产物转化 EN/JA gold 样本（Phase 10 预备）。

输入：
  docs/audit_tables/en_audit_results.json   — EN 86 hard cases
  docs/audit_tables/ja_audit_results.json   — JA 55 hard cases
  docs/audit_tables/ko_audit_results.json   — KO 已存在 gold（51）
  benchmarks/en_reaction_benchmark.json     — 基础 entries（226）
  benchmarks/ja_reaction_benchmark.json     — 基础 entries（171）

输出（不覆盖原文件，写 *_gold_enhanced.json）：
  把审计中"有明确正确译文判定"的 case 回填 gold_zh，并标注来源与置信度。
  仅把 error_type 明确、note 含可判定正确译文（应为/→/正确）的条目作为 gold，
  其余不动。绝不编造 gold——不知道的保持空。

用法：python tools/build_gold_from_audit.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
AUDIT_DIR = BASE / "docs" / "audit_tables"
BENCH_DIR = BASE / "benchmarks"

# 从 note 中提取"应为 X"的译文
_EXPECTED_RE = re.compile(r"(?:应为|正确译文|正确为|应为是|应该是|→|＝|==)\s*[：: ]?\s*([^（(；;，,。]+)")


def _extract_gold_from_note(note: str) -> str | None:
    """从审计 note 提取明确判定的正确译文；无法确定返回 None。"""
    if not note:
        return None
    # 明确排除"已修/污染/编造/猜义"等非 gold 语义
    if re.search(r"(已修|污染|编造|猜义|误报|可接受|疑为|疑似|串行|待音频|待确认)", note):
        return None
    # 明确排除来源性说明词
    for bad in ("ASR ", "display", "滚动", "round", "SOURCE"):
        if note.startswith(bad):
            return None
    # 优先"应为 X"模式（X 到左右括号/分号/逗号/句号/感叹/问号为止）
    m = re.search(r"应为\s*[「『\"']?([^」』\"'（）()；;，,。！？?]+)", note)
    if m:
        candidate = m.group(1).strip()
        if 1 <= len(candidate) <= 60:
            return candidate
    # 箭头模式：X → 正确译文（排除"已修/编造"语义）
    m = re.search(r"→\s*[「『\"']?([^」』\"'（）()；;，,。！？?]{2,60})", note)
    if m:
        candidate = m.group(1).strip()
        if candidate and not re.match(r"(?:display|ASR|滚动|round)", candidate):
            return candidate
    return None


def _load_audit(lang: str) -> dict:
    path = AUDIT_DIR / f"{lang}_audit_results.json"
    if not path.is_file():
        print(f"[skip] {path.name} 不存在")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_benchmark(lang: str) -> dict:
    path = BENCH_DIR / f"{lang}_reaction_benchmark.json"
    if not path.is_file():
        print(f"[skip] {path.name} 不存在")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_audit_gold(lang: str) -> tuple[int, int]:
    audit = _load_audit(lang)
    bench = _load_benchmark(lang)
    if not audit or not bench:
        return 0, 0
    entries = bench.get("entries", [])
    by_id = {str(e.get("id")): e for e in entries}
    cases = audit.get("cases", [])
    filled = 0
    candidates = []
    for case in cases:
        case_id = str(case.get("id"))
        entry = by_id.get(case_id)
        # id 可能是 "572.3-574.8|181" 形式，也尝试时间前缀匹配
        if entry is None:
            entry = next(
                (e for e in entries if str(e.get("id", "")).endswith(f"|{case_id}")),
                None,
            )
        if entry is None:
            continue
        if (entry.get("gold_zh") or "").strip():
            continue  # 已有 gold 不覆盖
        note = str(case.get("note", ""))
        gold = _extract_gold_from_note(note)
        if gold:
            entry["gold_zh"] = gold
            tags = list(entry.get("error_tags") or [])
            if "audit_gold" not in tags:
                tags.append(f"audit_gold:{case.get('error_type', '')}")
            entry["error_tags"] = tags
            filled += 1
            candidates.append((case_id, gold, note[:80]))
    out = BENCH_DIR / f"{lang}_reaction_benchmark.json"
    # 写增强版（保留原文件）
    enhanced = BENCH_DIR / f"{lang}_reaction_benchmark_gold_enhanced.json"
    bench["entries"] = entries
    enhanced.write_text(
        json.dumps(bench, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"=== {lang}: filled {filled} gold from audit ({len(entries)} total) ===")
    for cid, gold, note in candidates[:15]:
        print(f"  #{cid}: {gold}  <- {note}")
    return filled, len(entries)


def main() -> None:
    total = 0
    for lang in ("en", "ja"):
        filled, _ = _apply_audit_gold(lang)
        total += filled
    # KO 已 51 gold，只统计不动
    ko = _load_benchmark("ko")
    if ko:
        ko_gold = sum(1 for e in ko.get("entries", []) if (e.get("gold_zh") or "").strip())
        print(f"=== ko: 已有 {ko_gold} gold（不动） ===")
    print(f"\n总计新增 gold: {total}")
    print("增强文件写入 benchmarks/*_gold_enhanced.json")


if __name__ == "__main__":
    main()
