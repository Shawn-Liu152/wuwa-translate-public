#!/usr/bin/env python3
"""Whisper chunked transcription (finalblind) — one fresh process per chunk.

Root cause (Codex diagnosis + clip test 2026-08-11): long single-pass
transcription on this Windows box dies silently (APPCRASH, faulting
module MSVCP140.dll); short clips stay alive. So: split audio into
3-4 min chunks (with overlap), transcribe each chunk in a fresh python
process, merge with timestamp offsets, dedupe overlap text.

Usage: python tools/whisper_transcribe_chunked.py <audio> <out.srt> --language en
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

CHUNK_SEC = 240      # 4 分钟一块
OVERLAP_SEC = 15     # 重叠 15s 用于合并去重

CHUNKER = r'''import sys
import json
from faster_whisper import WhisperModel

audio, out_json, start_s, end_s, lang = sys.argv[1:6]
model = WhisperModel('large-v3', device='cpu', compute_type='int8', cpu_threads=4)
segments, info = model.transcribe(
    audio, language=lang, vad_filter=False, beam_size=5,
    clip_timestamps=f'{start_s},{end_s}',
)
rows = []
for seg in segments:
    text = (seg.text or '').strip()
    if not text:
        continue
    rows.append({'start': seg.start, 'end': seg.end, 'text': text})
open(out_json, 'w', encoding='utf-8').write(json.dumps(rows, ensure_ascii=False))
print(f'done {len(rows)} segs', flush=True)
'''


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms2:03d}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="input mp3 path")
    ap.add_argument("out_srt", help="output srt path")
    ap.add_argument("--language", default="en")
    ap.add_argument("--duration", type=float, default=None,
                    help="audio duration seconds (auto if omitted)")
    args = ap.parse_args()

    audio = Path(args.audio)
    if not audio.is_file():
        print(f"[error] audio not found: {audio.name}")
        return 1

    # 探测时长（ffprobe）
    duration = args.duration
    if duration is None:
        import subprocess as sp
        r = sp.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "json", str(audio)],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            duration = float(json.loads(r.stdout)["format"]["duration"])
        else:
            print("[error] ffprobe failed; details omitted")
            return 1
    print(f"[info] audio duration = {duration:.0f}s, chunk={CHUNK_SEC}s overlap={OVERLAP_SEC}s")

    # 切块
    chunks = []
    start = 0.0
    while start < duration:
        end = min(start + CHUNK_SEC, duration)
        chunks.append((start, end))
        if end >= duration:
            break
        start = end - OVERLAP_SEC

    python = sys.executable
    all_rows = []  # (start_sec, end_sec, text)
    failed_chunks = []
    for i, (cs, ce) in enumerate(chunks):
        descriptor, raw_chunk_out = tempfile.mkstemp(
            suffix=".json", prefix=f"whisper_chunk{i}_"
        )
        chunk_out = Path(raw_chunk_out)
        try:
            os.close(descriptor)
            descriptor = -1
            cmd = [python, "-u", "-c", CHUNKER,
                   str(audio), str(chunk_out), str(int(cs)), str(int(ce)), args.language]
            print(f"[chunk {i+1}/{len(chunks)}] {cs:.0f}-{ce:.0f}s ...", flush=True)
            t0 = time.time()
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                failed_chunks.append((cs, ce, "timeout"))
                print(f"[chunk {i+1}] TIMEOUT", flush=True)
                continue
            if r.returncode != 0:
                print(
                    f"[chunk {i+1}] FAILED rc={r.returncode}; details omitted",
                    flush=True,
                )
                print(f"[chunk {i+1}] retrying once ...", flush=True)
                try:
                    r = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=1800
                    )
                except subprocess.TimeoutExpired:
                    failed_chunks.append((cs, ce, "timeout"))
                    print(
                        f"[chunk {i+1}] TIMEOUT on retry — skip chunk",
                        flush=True,
                    )
                    continue
                if r.returncode != 0:
                    failed_chunks.append((cs, ce, f"rc={r.returncode}"))
                    print(f"[chunk {i+1}] FAILED twice — skip chunk", flush=True)
                    continue
            try:
                rows = json.loads(chunk_out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                failed_chunks.append((cs, ce, "invalid_json"))
                print(f"[chunk {i+1}] INVALID OUTPUT — skip chunk", flush=True)
                continue
            # Codex review fix (2026-08-11): faster-whisper clip_timestamps
            # returns ABSOLUTE timestamps in the original audio — do NOT add cs.
            # (verified against faster_whisper/transcribe.py clip logic)
            for row in rows:
                rs = float(row["start"])
                re_ = float(row["end"])
                text = row["text"]
                all_rows.append((rs, re_, text))
            print(
                f"[chunk {i+1}] {len(rows)} segs in {time.time()-t0:.0f}s",
                flush=True,
            )
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            chunk_out.unlink(missing_ok=True)

    if not all_rows:
        print("[error] no segments produced")
        return 1

    if failed_chunks:
        print(f"[error] {len(failed_chunks)} chunks failed: {failed_chunks}")
        return 2

    # 排序 + 重叠去重：仅当文本高度相似（同一句在重叠区重复）才合并；
    # 同时说话/相邻不同内容必须都保留（Codex review fix）。
    all_rows.sort(key=lambda x: x[0])
    merged = []
    for rs, re_, text in all_rows:
        if merged and rs < merged[-1][1] - 0.5:
            prs, pre, ptext = merged[-1]
            # 归一化后比较：去标点小写；Jaro/子串近似
            def _norm(t):
                return ''.join(c for c in t.lower() if c.isalnum())
            a, b = _norm(ptext), _norm(text)
            sim = 0.0
            if a and b:
                shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                if shorter in longer:
                    sim = len(shorter) / len(longer)
                else:
                    # 公共子串比例（简化）
                    best = 0
                    for k in range(min(len(shorter), 40), 5, -1):
                        if shorter[:k] in longer or longer[:k] in shorter:
                            best = k
                            break
                    sim = best / max(len(a), len(b))
            if sim >= 0.7:
                # 同一内容重复 → 合并取更长
                if len(text) > len(ptext):
                    merged[-1] = (prs, max(pre, re_), text)
                else:
                    merged[-1] = (prs, max(pre, re_), ptext)
            else:
                merged.append((rs, re_, text))
        else:
            merged.append((rs, re_, text))

    # 写 SRT
    lines = []
    for i, (rs, re_, text) in enumerate(merged, 1):
        lines.append(f"{i}\n{_ts(rs)} --> {_ts(re_)}\n{text}\n")
    Path(args.out_srt).write_text("\n".join(lines), encoding="utf-8")
    print(f"[done] {len(merged)} cues -> {Path(args.out_srt).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
