#!/usr/bin/env python3
"""ASR candidate 自动发现（任务书 #26）— OFFLINE 只读审计。

从真实 corpus（download/holdout-*/、download/rerun-*/）扫描"疑似专名"：
- 在 glossary / asr_corrections 中未命中
- 与 glossary 已知词有高编辑相似度（音译/拼写变体候选）
- 或出现频率高（>=3）的疑似专名形态

输出 reports/asr_candidates_auto.json —— **只输出候选与 confidence，
绝不自动写入 corrections**（人工 adjudicate 后手动入库）。

用法：
  python tools/audit_asr_candidates.py [--lang ko|ja|en] [--min-freq 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

CORPUS_DIRS = [
    BASE / "download" / "holdout-6fN9ii-8U7s",   # EN
    BASE / "download" / "holdout-l9NCconvSNA",   # JA
    BASE / "download" / "holdout-a8ZSJPGGbbg",   # KO
    BASE / "download" / "rerun-en-20260810",
    BASE / "download" / "rerun-ja-20260810",
    BASE / "download" / "rerun-ko-20260810",
]

SRT_FILES = {
    "en": ["en.en.srt", "final.zh.en.union.final.srt", "reference.whisper.en.srt"],
    "ja": ["ja.ja.srt", "final.zh.ja.union.final.srt", "reference.whisper.ja.srt"],
    "ko": ["ko.ko.srt", "final.zh.ko.union.final.srt", "reference.whisper.ko.srt"],
}

# 韩文音素近似组（用于音译变体判断）
KO_SOUND_GROUPS = [
    {"에", "애"}, {"이", "에"}, {"래", "레"}, {"봐", "바"}, {"돼", "되"},
    {"게", "개"}, {"네", "내"}, {"워", "어"}, {"와", "아"}, {"쇼", "수"},
    {"즈", "스"}, {"제", "지"}, {"치", "찌"}, {"시", "씨"}, {"켄", "겐"},
    {"텍", "택"}, {"느", "으"}, {"니", "이"}, {"야", "아"},
]

JA_SOUND_GROUPS = [
    {"ー", ""}, {"ッ", ""}, {"ウ", "オ"}, {"オ", "ウ"}, {"ア", "ヤ"},
    {"ヴ", "ブ"}, {"ジ", "チ"}, {"ズ", "ス"}, {"ツ", "チ"}, {"ハ", "ファ"},
    {"ワ", "ア"}, {"ヨ", "オ"}, {"ュ", ""}, {"ィ", "イ"},
]

HIRAGANA_KATAKANA = re.compile(r"[\u3040-\u30ff]")


def _ko_normalize(text: str) -> str:
    """韩文：去掉助词/空格/标点，保留名词干近似。"""
    t = re.sub(r"[^가-힣]", "", text)
    return t


def _ja_normalize(text: str) -> str:
    t = re.sub(r"[^ぁ-んァ-ン]", "", text)
    return t


def _en_normalize(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _edit_similarity(a: str, b: str) -> float:
    """归一化编辑距离相似度（Levenshtein）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if abs(la - lb) > max(la, lb) * 0.5:
        return 0.0
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i]
        for j in range(1, lb + 1):
            cur.append(min(
                prev[j] + 1, cur[j - 1] + 1,
                prev[j - 1] + (a[i - 1] != b[j - 1]),
            ))
        prev = cur
    return 1.0 - prev[lb] / max(la, lb)


def _extract_candidates(text: str, lang: str) -> list[str]:
    """提取疑似专名词元（不依赖词表）。"""
    out = set()
    if lang == "ko":
        for m in re.finditer(r"[가-힣]{2,6}", text):
            out.add(m.group(0))
    elif lang == "ja":
        for m in re.finditer(r"[ァ-ン]{2,8}", text):  # 片假名 = 外来语/专名
            out.add(m.group(0))
    else:
        for m in re.finditer(r"[A-Za-z]{3,12}", text):
            out.add(m.group(0))
    return list(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["en", "ja", "ko"], required=True)
    ap.add_argument("--min-freq", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    lang = args.lang

    # 1. 收集 glossary 已知词（避免误报）
    known = set()
    try:
        import sqlite3
        conn = sqlite3.connect(str(BASE / "data" / "glossary.db"))
        cur = conn.cursor()
        for row in cur.execute(
            "SELECT english, source_term FROM glossary WHERE source_language=?",
            (lang,),
        ):
            known.add(str(row[0]).casefold())
            known.add(str(row[1]).casefold())
        conn.close()
    except Exception as exc:  # noqa: BLE001
        try:
            import sqlite3
            conn = sqlite3.connect(str(BASE / "data" / "glossary.db"))
            cur = conn.cursor()
            tables = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            print(f"[warn] glossary 表结构未知（tables={tables}），改用全表扫描")
            for table in tables:
                cols = [c[1] for c in cur.execute(f"PRAGMA table_info({table})")]
                for col in cols:
                    if col in ("term", "english", "name", "word", "value"):
                        for row in cur.execute(f"SELECT {col} FROM {table}"):
                            known.add(str(row[0]).casefold())
            conn.close()
        except Exception as exc2:  # noqa: BLE001
            print(f"[warn] glossary 加载失败: {exc} / {exc2}（将无法排除已知词）")

    # 2. 已知 corrections
    known_corr = set()
    corr_path = BASE / "data" / (
        "asr_corrections_en.json" if lang == "en" else "asr_corrections.json")
    try:
        corr = json.loads(corr_path.read_text(encoding="utf-8"))
        for k, v in (corr.items() if isinstance(corr, dict) else []):
            known_corr.add(str(k).casefold())
            known_corr.add(str(v).casefold())
    except Exception:  # noqa: BLE001
        pass

    # 3. 扫描 corpus
    freq: dict[str, int] = {}
    for d in CORPUS_DIRS:
        if not d.is_dir():
            continue
        for fname in SRT_FILES.get(lang, []):
            p = d / fname
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            for cand in _extract_candidates(text, lang):
                norm = (cand.casefold() if lang == "en" else cand)
                freq[norm] = freq.get(norm, 0) + 1

    # 4. 过滤 + 相似度打分
    candidates = []
    freq_only = []
    for cand, count in sorted(freq.items(), key=lambda x: -x[1]):
        if count < args.min_freq:
            continue
        if cand in known or cand in known_corr:
            continue
        if lang == "en" and re.fullmatch(r"(dps|hp|boss|pv|ok|max|min|kuro|like|wait|bro|her|that|this|look|okay|yeah|oh|god|just|what|now|right|come|going|got|think|know|really)", cand):
            continue
        # 与已知词找相似
        best_sim, best_match = 0.0, ""
        for known_word in known:
            if lang == "en":
                sim = _edit_similarity(cand, known_word)
            else:
                sim = _edit_similarity(_ko_normalize(cand), _ko_normalize(known_word)) \
                    if lang == "ko" else \
                    _edit_similarity(_ja_normalize(cand), _ja_normalize(known_word))
            if sim > best_sim:
                best_sim, best_match = sim, known_word
        confidence = "high" if best_sim >= 0.75 else \
                     "medium" if best_sim >= 0.6 else "low"
        entry = {
            "candidate": cand,
            "frequency": count,
            "best_known_match": best_match,
            "similarity": round(best_sim, 3),
            "confidence": confidence,
        }
        if best_sim >= 0.6:
            candidates.append(entry)  # 变体候选（与已知术语相似）
        else:
            freq_only.append(entry)   # 高频未知词（需人工判断是否专名）

    out_path = BASE / "reports" / f"asr_candidates_auto_{lang}.json"
    payload = {
        "schema_version": 1,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "language": lang,
        "corpus_dirs": [str(d) for d in CORPUS_DIRS if d.is_dir()],
        "known_terms_excluded": len(known) + len(known_corr),
        "note": "只输出候选，不自动写入 corrections；人工 adjudicate 后手动入库。",
        "candidates": candidates,
        "high_frequency_unknown": freq_only[:20],
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:3000])
    else:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[ok] {len(candidates)} variant candidates -> {out_path}")
        for c in candidates[:15]:
            print(f"  {c['candidate']} x{c['frequency']} ~ {c['best_known_match']} ({c['similarity']}, {c['confidence']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
