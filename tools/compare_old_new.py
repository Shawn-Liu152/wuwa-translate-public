#!/usr/bin/env python3
"""OLD vs NEW 对比：rerun 产物 vs 原始 Job 产物（确定性指标，不依赖人工判断）。

用法：python tools/compare_old_new.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def parse_srt_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        body = " ".join(lines[2:]).strip()
        if body:
            cues.append({"time": lines[1], "text": body})
    return cues


def main() -> None:
    jobs = {
        "en": {"old_dir": "2519dcb3843e", "video": "lWgwc_xNzrg"},
        "ja": {"old_dir": "df93e43d0c47", "video": "gl8vjOL1xWI"},
        "ko": {"old_dir": "2170758a3e78", "video": "p0bHZydkDrA"},
    }
    report = {}
    for lang, spec in jobs.items():
        old_final = BASE / "download" / spec["old_dir"] / "final.zh.srt"
        new_dir = BASE / "download" / f"rerun-{lang}-20260810"
        new_final = new_dir / "final.zh.srt"
        new_union = new_dir / f"final.{lang if lang=='en' else 'zh'}.{lang if lang!='en' else 'en'}.union.final.srt"
        # union 命名：legacy_prefix + .{lang}.union.final.srt
        new_union = new_dir / f"final.zh.{lang}.union.final.srt"
        new_r2 = new_dir / "final.zh.round2.zh.srt"
        new_risk = new_dir / "final.zh.risk.generated.json"

        row = {"lang": lang, "old_final_exists": old_final.exists(),
               "new_final_exists": new_final.exists()}
        if not (old_final.exists() and new_final.exists()):
            report[lang] = row
            continue

        old_cues = parse_srt_file(old_final)
        new_cues = parse_srt_file(new_final)
        row.update({
            "old_final_cues": len(old_cues),
            "new_final_cues": len(new_cues),
            # >> 说话人内容保留（修复 F1 的直接指标）
            "old_arrow_in_final": sum(1 for c in old_cues if ">>" in c["text"]),
            "new_arrow_in_final": sum(1 for c in new_cues if ">>" in c["text"]),
            # 原文残留（AMS 等）
            "old_source_residue": sum(1 for c in old_cues if re.search(r"[a-zA-Z\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3040-\u30ff]", c["text"])),
            "new_source_residue": sum(1 for c in new_cues if re.search(r"[a-zA-Z\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3040-\u30ff]", c["text"])),
        })
        # Reviewer 执行度
        if new_risk.exists():
            try:
                risk = json.loads(new_risk.read_text(encoding="utf-8"))
                items = risk.get("items", [])
                eligible = sum(1 for it in items
                               if not str(it.get("defer_reason", "")).startswith("deferred"))
                row.update({"new_risk_total": len(items),
                            "new_risk_non_deferred": eligible})
            except Exception as e:
                row["new_risk_parse_error"] = str(e)[:60]
        # Round2 产物
        row["new_round2_exists"] = new_r2.exists()
        # union 中 AMS 归一（KO 专项）
        if lang == "ko" and new_union.exists():
            union_text = new_union.read_text(encoding="utf-8-sig")
            row["new_union_ams_left"] = len(re.findall(r"\bAMS\b", union_text))
            row["new_union_aimeus"] = len(re.findall(r"에이메스", union_text))
        report[lang] = row

    out = BASE / "docs" / "audit_tables" / "old_vs_new_compare.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
