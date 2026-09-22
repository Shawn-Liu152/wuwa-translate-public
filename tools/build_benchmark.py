# -*- coding: utf-8 -*-
"""构建韩语 Reaction 金标 Benchmark（一次性工具）。

数据来源：final8 验收成品（download/1033e783298f）+ 仲裁 JSON + union 双源。
- gold_zh 只在有把握时填写（来自 final8 已人工审校的正确译文）；
- ASR 冲突且无法确认的条目不伪造 gold，只标 error_tags + canonical 或双源。
- 类别覆盖：专名句 / 普通口语 / ASR 冲突 / 脏话 / 数字 / 否定 / 反问。
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DL = os.path.join(BASE, "download", "1033e783298f")


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_srt_text(content):
    """SRT → [(start_ms, end_ms, text)]"""
    cues = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        if not lines[0].isdigit():
            continue
        time_line = lines[1]
        m = re.match(r"(\d\d:\d\d:\d\d,\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d,\d\d\d)", time_line)
        if not m:
            continue
        text = " ".join(lines[2:])
        start = _to_ms(m.group(1))
        end = _to_ms(m.group(2))
        cues.append((start, end, text))
    return cues


def _to_ms(ts):
    h, m, s, ms = ts.replace(",", ":").split(":")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def main():
    final8 = parse_srt_text(read_text(os.path.join(DL, "final.zh.srt")))
    arbitration = json.load(open(
        os.path.join(DL, "过程文件", "final.zh.ko.union.final.srt.arbitration.json"),
        encoding="utf-8-sig",
    ))
    union_cues = parse_srt_text(read_text(
        os.path.join(DL, "过程文件", "final.zh.ko.union.final.srt"),
    ))

    # union → arbitration 映射（按时间）
    arb_by_time = {}
    for unit in arbitration.values():
        arb_by_time[(unit["start_ms"], unit["end_ms"])] = unit

    # final8 译文 → 时间映射
    zh_by_time = {(s, e): t for s, e, t in final8}

    entries = []
    for s, e, canonical in union_cues:
        unit = arb_by_time.get((s, e))
        if not unit:
            continue
        zh = zh_by_time.get((s, e), "")
        entry = {
            "id": unit["key"],
            "start_ms": s,
            "end_ms": e,
            "youtube_text": unit.get("primary_evidence", ""),
            "whisper_text": unit.get("secondary_evidence", ""),
            "canonical_source_text": unit.get("canonical_source_text", canonical),
            "gold_zh": "",
            "entities": [],
            "error_tags": [],
        }
        # 专名句打标（去空格匹配：'에이 메스잖아' 也算 에이메스）
        entity_map = {
            "에이메스": "爱弥斯", "히유키": "绯雪", "데니아": "达妮娅",
            "스트라이더 게이트": "隧门", "엑소스트라이더": "隧者",
            "보이드 스톰": "虚质磁暴", "잔성회": "残星会", "로야": "罗伊",
            "라하이네로": "拉海洛",
        }
        compact = re.sub(r"\s+", "", entry["canonical_source_text"])
        for src, zh_name in entity_map.items():
            if re.sub(r"\s+", "", src) in compact:
                entry["entities"].append({"source": src, "target": zh_name})
        # error tags
        if unit.get("source_conflict"):
            entry["error_tags"].append("asr_conflict")
        if unit.get("source_decision") == "unresolved":
            entry["error_tags"].append("unresolved")
        if (unit.get("source_decision") == "unresolved"
                and unit.get("unknown_entities")):
            entry["error_tags"].append("unknown_entity")
        entries.append(entry)

    # gold_zh 标注：只对有把握的（final8 译文 + 非 unresolved + 无未知专名）
    for entry in entries:
        if "unresolved" in entry["error_tags"] or "unknown_entity" in entry["error_tags"]:
            continue
        text = zh_by_time.get((entry["start_ms"], entry["end_ms"]), "")
        if text and len(text) <= 60:
            entry["gold_zh"] = text

    out = {
        "name": "ko-reaction2-benchmark",
        "description": "鸣潮韩语 Reaction 金标（final8 验收提取，2026-08-06）",
        "source_language": "ko",
        "target_language": "zh-CN",
        "entries": entries,
    }
    path = os.path.join(BASE, "benchmarks", "ko_reaction_benchmark.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"写入 {path}")
    print(f"总条目: {len(entries)}")
    print(f"有 gold_zh: {sum(1 for e in entries if e['gold_zh'])}")
    tags = {}
    for e in entries:
        for t in e["error_tags"]:
            tags[t] = tags.get(t, 0) + 1
    print(f"error_tags 分布: {tags}")


if __name__ == "__main__":
    main()
