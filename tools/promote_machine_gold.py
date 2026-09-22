"""Phase 5: append Final Blind reviewer-keep cases as machine_confirmed gold.

Appends new entries to benchmarks/{lang}_reaction_benchmark.json with
gold_status='machine_confirmed' (distinct from provisional/confirmed).
Machine-confirmed = Reviewer keep + confidence >= 0.9 + no blocking risks.
"""
import json
import re
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
         # Codex review fix: entity risks must block gold promotion
         # (a case flagged unknown_entity/hallucinated_entity is not
         # trustworthy enough to become machine-confirmed gold)
         'unknown_entity', 'hallucinated_entity', 'suspected_name',
         'person_name_present'}

total_new = 0
for lang, jobdir in JOBS.items():
    d = BASE / 'download' / jobdir
    r2 = json.loads((d / 'final.zh.round2.results.json').read_text(encoding='utf-8'))
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
    existing_src = {e.get('canonical_source_text', '').strip().lower() for e in entries}

    added = 0
    next_id = max(len(entries) + 1, 1000)
    for it in r2['items']:
        if it.get('decision') != 'keep':
            continue
        try:
            conf = float(it.get('confidence', 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.9:
            continue
        k = str(it.get('key', ''))
        tail = k.split(':', 1)[-1] if ':' in k else k
        src, reasons = src_by_tail.get(tail, ('', []))
        if not src:
            continue
        # 长度门槛：EN 按词，JA/KO 按字符（无空格分词）
        if lang == 'en' and len(src.split()) < 4:
            continue
        if lang in ('ja', 'ko') and len(src) < 6:
            continue
        if src.lower() in existing_src:
            continue
        if any(r.split(':')[0] in BLOCK for r in reasons):
            continue
        tgt = (it.get('round2_text') or '').strip()
        if not tgt:
            continue
        # 时间解析（key 格式 ts|ts|n）
        m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\|(\d{2}):(\d{2}):(\d{2}),(\d{3})', tail)
        start_ms = end_ms = 0
        if m:
            start_ms = int(m.group(1))*3600000 + int(m.group(2))*60000 + int(m.group(3))*1000 + int(m.group(4))
            end_ms = int(m.group(5))*3600000 + int(m.group(6))*60000 + int(m.group(7))*1000 + int(m.group(8))
        entries.append({
            'id': next_id,
            'start_ms': start_ms,
            'end_ms': end_ms,
            'youtube_text': src,
            'whisper_text': '',
            'canonical_source_text': src,
            'gold_zh': tgt,
            'gold_status': 'machine_confirmed',
            'entities': [],
            'error_tags': [],
        })
        existing_src.add(src.lower())
        next_id += 1
        added += 1

    bench_path.write_text(json.dumps(bench, ensure_ascii=False, indent=1), encoding='utf-8')
    total_new += added
    from collections import Counter
    print(f'[{lang}] +{added} machine_confirmed gold appended')
    print(f'  distribution: {dict(Counter(e.get("gold_status","none") for e in entries))}')

print(f'total new machine_confirmed: {total_new}')
