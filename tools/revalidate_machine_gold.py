"""Re-validate machine_confirmed gold with the tightened BLOCK set.

Downgrades (to 'provisional') any machine_confirmed entry whose risk set
now contains a blocked reason (entity risks added by Codex review).
Data is preserved — only the status label changes.
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

JOBS = {
    'en': 'finalblind-oRkLMSh9F1Y',
    'ja': 'finalblind-9UyrQE1qBDg',
    'ko': 'finalblind-C4Tf5vRMmGc',
}
BLOCK = {'negation_missing', 'condition_marker_missing', 'incomplete_fragment',
         'empty_translation', 'text_conflict', 'source_unresolved',
         'unknown_entity', 'hallucinated_entity', 'suspected_name',
         'person_name_present'}

total_downgraded = 0
for lang, jobdir in JOBS.items():
    d = BASE / 'download' / jobdir
    risk_items = json.loads((d / 'final.zh.risk.generated.json').read_text(encoding='utf-8')).get('items', [])
    src_by_tail = {}
    for item in risk_items:
        src = (item.get('source_text') or '').strip()
        if not src:
            continue
        k = str(item.get('key') or item.get('id') or '')
        tail = k.split(':', 1)[-1] if ':' in k else k
        src_by_tail[tail] = (src, item.get('reasons', []))

    bench_path = BASE / 'benchmarks' / f'{lang}_reaction_benchmark.json'
    bench = json.loads(bench_path.read_text(encoding='utf-8'))
    entries = bench['entries']
    downgraded = 0
    for e in entries:
        if e.get('gold_status') != 'machine_confirmed':
            continue
        src = (e.get('canonical_source_text') or '').strip()
        # find risks by source match
        reasons = []
        for s, r in src_by_tail.values():
            if s == src or s.lower() == src.lower():
                reasons = r
                break
        if any(r.split(':')[0] in BLOCK for r in reasons):
            e['gold_status'] = 'provisional'
            downgraded += 1
    bench_path.write_text(json.dumps(bench, ensure_ascii=False, indent=1), encoding='utf-8')
    total_downgraded += downgraded
    from collections import Counter
    print(f'[{lang}] downgraded {downgraded} -> '
          f'{dict(Counter(e.get("gold_status","none") for e in entries))}')

print(f'total downgraded: {total_downgraded}')
