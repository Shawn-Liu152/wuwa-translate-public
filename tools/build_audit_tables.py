#!/usr/bin/env python3
"""Build per-language full-chain audit tables from real job artifacts.

For each of the three real jobs (EN/JA/KO), align every stage:
raw -> cleaned -> whisper -> aligned -> union/canonical -> round1 -> round2 -> final
and write compact audit tables (one row per semantic unit) to
docs/audit_tables/<lang>_chain_audit.tsv for the audit workers.

Usage: python tools/build_audit_tables.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT_DIR = BASE / "docs" / "audit_tables"

TS_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def ts_to_ms(ts: str) -> int:
    m = TS_RE.search(ts or "")
    if not m:
        return -1
    h, mi, s, ms = (int(x) for x in m.groups())
    return ((h * 3600 + mi * 60 + s) * 1000) + ms


@dataclass
class Cue:
    idx: int
    start_ms: int
    end_ms: int
    text: str


def parse_srt(path: Path) -> list[Cue]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        m = re.search(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)", lines[1])
        if not m:
            continue
        body = " ".join(lines[2:]).strip()
        cues.append(Cue(idx, ts_to_ms(m.group(1)), ts_to_ms(m.group(2)), body))
    return cues


def load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return None


def pick_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    jobs = {
        "en": {
            "job": BASE / "download" / "2519dcb3843e",
            "video": "lWgwc_xNzrg",
            "pfx": "final.zh",
            "suffix": "en",
            "raw": "lWgwc_xNzrg.en.srt",
            "cleaned": "lWgwc_xNzrg.en.cleaned.srt",
            "whisper": "reference.whisper.en.srt",
            "aligned": "youtube.aligned.en.srt",
            "union": "final.zh.en.union.final.srt",
            "round1": "final.round1.zh.srt.translated-only.srt",
            "round1_full": "final.round1.zh.srt",
            "round2": "final.round2.zh.srt",
            "final": "final.zh.srt",
            "arbitration": None,
            "reconstruction": None,
            "display_map": "final.zh.display-map.json",
            "risk": "final.zh.risk.generated.json",
            "round2_results": "final.zh.round2.results.json",
            "pipeline_metrics": "final.zh.pipeline.metrics.json",
        },
        "ja": {
            "job": BASE / "download" / "df93e43d0c47",
            "video": "gl8vjOL1xWI",
            "pfx": "final.zh",
            "suffix": "ja",
            "raw": "gl8vjOL1xWI.ja-orig.srt",
            "cleaned": "gl8vjOL1xWI.ja.cleaned.srt",
            "whisper": None,
            "aligned": None,
            "union": "final.zh.ja.union.final.srt",
            "round1": "final.round1.zh.srt",
            "round1_full": None,
            "round2": "final.round2.zh.srt",
            "final": "final.zh.srt",
            "arbitration": "final.zh.ja.union.final.srt.arbitration.json",
            "reconstruction": "final.zh.reconstruction.json",
            "display_map": "final.zh.display-map.json",
            "risk": "final.zh.risk.generated.json",
            "round2_results": "final.zh.round2.results.json",
            "pipeline_metrics": "final.zh.pipeline.metrics.json",
        },
        "ko": {
            "job": BASE / "download" / "2170758a3e78",
            "video": "p0bHZydkDrA",
            "pfx": "final.zh",
            "suffix": "ko",
            "raw": "p0bHZydkDrA.ko-orig.srt",
            "cleaned": "p0bHZydkDrA.ko.cleaned.srt",
            "whisper": None,
            "aligned": None,
            "union": "final.zh.ko.union.final.srt",
            "round1": "final.round1.zh.srt",
            "round1_full": None,
            "round2": "final.round2.zh.srt",
            "final": "final.zh.srt",
            "arbitration": "final.zh.ko.union.final.srt.arbitration.json",
            "reconstruction": None,
            "display_map": "final.zh.display-map.json",
            "risk": "final.zh.risk.generated.json",
            "round2_results": "final.zh.round2.results.json",
            "pipeline_metrics": "final.zh.pipeline.metrics.json",
        },
    }

    summary = {}
    for lang, cfg in jobs.items():
        pf = cfg["job"] / "过程文件"
        jr = cfg["job"]
        rows = []

        def p(name, root=None):
            base = root if root is not None else pf
            return base / name if name else None

        raw = parse_srt(p(cfg["raw"]))
        cleaned = parse_srt(p(cfg["cleaned"]))
        whisper = parse_srt(p(cfg["whisper"])) if cfg["whisper"] else []
        aligned = parse_srt(p(cfg["aligned"])) if cfg["aligned"] else []
        union = parse_srt(p(cfg["union"]))
        round1 = parse_srt(p(cfg["round1"]))
        round2 = parse_srt(p(cfg["round2"]))
        # final.zh.srt lives in the job root (cleanup rule keeps only
        # final + job.json there), NOT in 过程文件/
        final = parse_srt(p(cfg["final"], root=jr))

        arb = load_json(p(cfg["arbitration"])) if cfg["arbitration"] else None
        recon = load_json(p(cfg["reconstruction"])) if cfg["reconstruction"] else None
        dm = load_json(p(cfg["display_map"]))
        risk = load_json(p(cfg["risk"]))
        r2r = load_json(p(cfg["round2_results"]))
        metrics = load_json(p(cfg["pipeline_metrics"]))

        # risk map: subtitle_id -> item
        risk_map = {}
        if risk and isinstance(risk.get("items"), list):
            for it in risk["items"]:
                risk_map[it.get("subtitle_id")] = it
        # round2 decision map: key -> item
        r2_map = {}
        if r2r and isinstance(r2r.get("items"), list):
            for it in r2r["items"]:
                r2_map[it.get("key")] = it

        # For each union unit (the semantic unit fed to translation), align
        for u in union:
            start, end = u.start_ms, u.end_ms
            # raw cue that overlaps most
            def best(cues):
                bestc, bestov = None, -1
                for c in cues:
                    ov = min(end, c.end_ms) - max(start, c.start_ms)
                    if ov > bestov:
                        bestov, bestc = ov, c
                return bestc

            r = best(raw)
            cl = best(cleaned)
            ws = best(whisper)
            al = best(aligned)
            r1 = best(round1)
            r2 = best(round2)
            fn = best(final)

            arb_info = ""
            if arb:
                for key, unit in arb.items():
                    if unit.get("start_ms") == start and unit.get("end_ms") == end:
                        arb_info = (
                            f"decision={unit.get('source_decision')}|conf={unit.get('source_confidence')}"
                            f"|conflict={unit.get('source_conflict')}|canonical={pick_text(unit.get('canonical_source_text'))}"
                        )
                        break

            recon_info = ""
            if recon and isinstance(recon, dict):
                for key, unit in recon.items():
                    if not isinstance(unit, dict):
                        continue
                    if unit.get("start_ms") == start and unit.get("end_ms") == end:
                        recon_info = (
                            f"status={unit.get('status')}|eligible={unit.get('eligible')}"
                            f"|resolved={unit.get('resolved')}|applied={unit.get('applied')}"
                            f"|rebuilt={pick_text(unit.get('rebuilt_text') or unit.get('canonical_after') or '')}"
                        )
                        break

            risk_item = risk_map.get(u.idx) or risk_map.get(str(u.idx))
            risk_info = ""
            if risk_item:
                risk_info = (
                    f"score={risk_item.get('score')}|severity={risk_item.get('severity')}"
                    f"|reasons={';'.join(risk_item.get('reasons') or [])[:120]}"
                )

            r2_item = r2_map.get(f"arbitration:{start}|{end}|{u.idx}")
            r2_info = ""
            if r2_item:
                r2_info = (
                    f"status={r2_item.get('round2_status')}|decision={r2_item.get('decision')}"
                    f"|replace={pick_text(r2_item.get('replacement') or r2_item.get('revised_text') or '')}"
                )

            rows.append({
                "id": u.idx,
                "time": f"{start/1000:.1f}-{end/1000:.1f}",
                "raw": pick_text(r.text) if r else "",
                "cleaned": pick_text(cl.text) if cl else "",
                "whisper": pick_text(ws.text) if ws else "",
                "aligned": pick_text(al.text) if al else "",
                "union": pick_text(u.text),
                "arb": arb_info,
                "recon": recon_info,
                "round1": pick_text(r1.text) if r1 else "",
                "round2": pick_text(r2.text) if r2 else "",
                "final": pick_text(fn.text) if fn else "",
                "risk": risk_info,
                "r2": r2_info,
            })

        header = ["id", "time", "raw", "cleaned", "whisper", "aligned", "union", "arb", "recon",
                  "round1", "round2", "final", "risk", "r2"]
        out_path = OUT_DIR / f"{lang}_chain_audit.tsv"
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\t".join(header) + "\n")
            for row in rows:
                fh.write("\t".join(str(row.get(h, "")) for h in header) + "\n")

        summary[lang] = {
            "rows": len(rows),
            "raw": len(raw), "cleaned": len(cleaned), "whisper": len(whisper),
            "union": len(union), "round1": len(round1), "round2": len(round2),
            "final": len(final),
            "arb_units": len(arb) if arb else 0,
            "display_map": len(dm) if dm else 0,
            "risk_items": len(risk.get("items", [])) if risk else 0,
            "round2_items": len(r2r.get("items", [])) if r2r else 0,
            "metrics": metrics,
        }
        print(f"[{lang}] wrote {out_path} with {len(rows)} rows")

    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "metrics"} for k, v in summary.items()}, indent=1))


if __name__ == "__main__":
    main()
