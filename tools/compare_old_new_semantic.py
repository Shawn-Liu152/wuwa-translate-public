"""Phase 8: semantic-level OLD vs NEW comparison augmentation.

For each historical job pair, pull round2 results + risk + final from
rerun dirs and compare error-class distributions (entity, negation,
question, hallucination, residual, reviewer outcomes).
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms

BASE = PROJECT_ROOT / 'download'

JOBS = {
    'en': ('rerun-en-20260810', '2519dcb3843e'),
    'ja': ('rerun-ja-20260810', 'df93e43d0c47'),
    'ko': ('rerun-ko-20260810', '2170758a3e78'),
}

ALLOW = {'DPS', 'HP', 'BOSS', 'PV', 'OK', 'MAX', 'MIN', 'BGM', 'Uber',
         'Excuse', 'Ending'}


def audit_final(path: Path) -> dict:
    subs = parse_srt(str(path))
    overlap = nonpos = 0
    for i, s in enumerate(subs):
        st, en = timestamp_to_ms(s.start), timestamp_to_ms(s.end)
        if en <= st:
            nonpos += 1
        if i and timestamp_to_ms(subs[i-1].end) > st:
            overlap += 1
    resid = []
    for s in subs:
        for m in re.finditer(r'[A-Za-z]{3,}', s.text):
            w = m.group(0)
            if w not in ALLOW and w.upper() not in ALLOW:
                resid.append(w)
    norm = [re.sub(r'[\W_]+', '', s.text) for s in subs]
    dups = sum(1 for t, n in Counter(norm).items() if n > 1 and len(t) > 4)
    return {
        'cues': len(subs), 'overlap': overlap, 'nonpositive': nonpos,
        'residual': len(resid), 'residual_words': list(set(resid))[:8],
        'dups': dups,
    }


def audit_round2(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    items = d.get('items', [])
    return {
        'decisions': dict(Counter(i.get('decision') for i in items)),
        'statuses': dict(Counter(i.get('round2_status') for i in items)),
    }


def audit_risk(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    reasons = Counter()
    for item in d.get('items', []):
        for r in item.get('reasons', []):
            reasons[r.split(':')[0]] += 1
    return dict(reasons.most_common(8))


print("=" * 80)
print("OLD vs NEW — semantic-level comparison (2026-08-11 Phase 8)")
print("=" * 80)
for lang, (new_dir, old_id) in JOBS.items():
    nd = BASE / new_dir
    od = BASE / old_id / '过程文件'
    print(f"\n### {lang.upper()}  {new_dir} (NEW) vs {old_id} (OLD)")
    # NEW final
    nf = nd / 'final.zh.srt'
    if nf.is_file():
        a = audit_final(nf)
        print(f"  NEW final: cues={a['cues']} overlap={a['overlap']} "
              f"nonpos={a['nonpositive']} residual={a['residual']} "
              f"dup={a['dups']}")
        if a['residual_words']:
            print(f"    residual words: {a['residual_words']}")
    # OLD final (if exists in 过程文件)
    of = od / 'final.zh.srt'
    if of.is_file():
        a = audit_final(of)
        print(f"  OLD final: cues={a['cues']} overlap={a['overlap']} "
              f"nonpos={a['nonpositive']} residual={a['residual']} "
              f"dup={a['dups']}")
    # Round2 / risk
    r2 = nd / 'final.zh.round2.results.json'
    if r2.is_file():
        print(f"  NEW round2: {audit_round2(r2)}")
    risk = nd / 'final.zh.risk.generated.json'
    if risk.is_file():
        print(f"  NEW risk: {audit_risk(risk)}")
