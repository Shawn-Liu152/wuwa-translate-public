import sys
from pathlib import Path

lang = sys.argv[1] if len(sys.argv) > 1 else 'ja'
job_root = Path(__file__).resolve().parents[1] / 'download' / f'holdout-{sys.argv[2]}'
audio = job_root / 'audio.mp3'
out = job_root / f'reference.whisper.{lang}.srt'

print(f'lang={lang} audio={audio}')
from faster_whisper import WhisperModel

model = WhisperModel('large-v3', device='cpu', compute_type='int8')
print('model ok')
segments, info = model.transcribe(
    str(audio), language=lang, vad_filter=False, beam_size=5)
print('lang:', info.language, info.language_probability)
lines = []
idx = 1
for seg in segments:
    text = (seg.text or '').strip()
    if not text:
        continue
    ms = int(round(seg.start * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    t1 = f'{h:02d}:{m:02d}:{s:02d},{ms2:03d}'
    ms = int(round(seg.end * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    t2 = f'{h:02d}:{m:02d}:{s:02d},{ms2:03d}'
    lines.append(f'{idx}\n{t1} --> {t2}\n{text}\n')
    idx += 1
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(f'done: {idx-1} cues -> {out}')
