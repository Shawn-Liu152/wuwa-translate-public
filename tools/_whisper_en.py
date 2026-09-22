import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOB_ROOT = PROJECT_ROOT / 'download' / 'holdout-6fN9ii-8U7s'
print('start')
from faster_whisper import WhisperModel
print('import ok')
model = WhisperModel('large-v3', device='cpu', compute_type='int8')
print('model ok')
segments, info = model.transcribe(
    str(JOB_ROOT / 'audio.mp3'),
    language='en', vad_filter=True, beam_size=5)
print('lang:', info.language, info.language_probability)
lines = []
idx = 1
for seg in segments:
    text = (seg.text or '').strip()
    if not text:
        continue
    start = seg.start
    end = seg.end
    ms = int(round(start * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    t1 = f'{h:02d}:{m:02d}:{s:02d},{ms2:03d}'
    ms = int(round(end * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    t2 = f'{h:02d}:{m:02d}:{s:02d},{ms2:03d}'
    lines.append(f'{idx}\n{t1} --> {t2}\n{text}\n')
    idx += 1
out = JOB_ROOT / 'reference.whisper.en.srt'
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(f'done: {idx-1} cues -> {out}')
