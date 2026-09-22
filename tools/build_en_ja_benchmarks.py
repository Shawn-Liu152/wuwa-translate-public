#!/usr/bin/env python3
"""Build EN/JA benchmark JSONs from real job artifacts (KO schema compatible).

从真实 Job 产物提取 hard cases 作为基准：
- EN: download/2519dcb3843e（lWgwc_xNzrg，226 units）
- JA: download/df93e43d0c47（gl8vjOL1xWI，171 units）

gold_zh 只填充有高置信正确译法且经人工/审计确认的条目；其余留空
（空 gold 只做 source 层回归，不做翻译正确性断言）。

Usage: python tools/build_en_ja_benchmarks.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "benchmarks"

# 人工/审计确认的高置信正确译法（id -> gold_zh），来自 2026-08-10 三语审计
EN_GOLD = {
    # Froolova -> 弗洛洛（Phrolova），round1 误译"那个人"
    "181": "漂泊者刚刚刺了弗洛洛？",
    # over 污染已修：full over 不是 full Rover
    "195": "已经全完了。天哪。",
}

JA_GOLD = {
    # unit 75: 完璧ではない...思い知ったわけでしょう。結構苦しいことを聞きます
    "75": "强烈地意识到自己并不完美吧。会听到相当痛苦的话。",
    # unit 76+77 合并：分かち合えるんだろうな
    "77": "在那里就有可以共鸣、可以分担的地方吧。所以这就是和绯雪的纠结碰撞的地方呢。",
}


def load_tsv(lang: str) -> list[dict]:
    rows = []
    with open(OUT.parent / "docs" / "audit_tables" / f"{lang}_chain_audit.tsv",
              encoding="utf-8") as f:
        rd = csv.DictReader(f, delimiter="\t")
        for r in rd:
            rows.append(r)
    return rows


def parse_time(t: str) -> int:
    """00:00:00,000 -> ms"""
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def build(lang: str, video: str, gold: dict) -> dict:
    rows = load_tsv(lang)
    entries = []
    for row in rows:
        start_ms = parse_time(row["time"].split("-")[0] + ",000") if False else 0
        # time 格式 "12.5-14.6"（秒），转 ms
        try:
            s, e = row["time"].split("-")
            start_ms = int(float(s) * 1000)
            end_ms = int(float(e) * 1000)
        except (ValueError, KeyError):
            start_ms, end_ms = 0, 0
        entities = []
        # 从 risk 列提取 unknown_entity 作为实体标记
        import re
        risk = row.get("risk", "")
        for m in re.finditer(r"unknown_entity:([^;|]+)", risk):
            entities.append({"source": m.group(1).strip(), "target": ""})
        entries.append({
            "id": f"{row['time']}|{row['id']}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "youtube_text": row.get("raw", ""),
            "whisper_text": row.get("whisper", ""),
            "canonical_source_text": row.get("union", ""),
            "gold_zh": gold.get(str(row["id"]), ""),
            "entities": entities,
            "error_tags": [],
        })
    return {
        "name": f"{lang}-reaction-audit-benchmark",
        "description": f"鸣潮{lang.upper()} Reaction 审计基准（2026-08-10 真实 Job {video}）",
        "source_language": lang,
        "target_language": "zh-CN",
        "entries": entries,
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    en = build("en", "lWgwc_xNzrg", EN_GOLD)
    ja = build("ja", "gl8vjOL1xWI", JA_GOLD)
    for name, data in (("en_reaction_benchmark.json", en),
                       ("ja_reaction_benchmark.json", ja)):
        path = OUT / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        gold = sum(1 for e in data["entries"] if e["gold_zh"])
        print(f"{name}: {len(data['entries'])} entries, {gold} gold")
        # 校验
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["source_language"] == data["source_language"]


if __name__ == "__main__":
    main()
