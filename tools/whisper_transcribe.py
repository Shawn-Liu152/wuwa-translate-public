#!/usr/bin/env python3
"""Holdout 视频 Whisper 转写（Phase 12，faster-whisper large-v3）。

用法：
  python tools/whisper_transcribe.py <audio.mp3> <out.srt> [--model large-v3]

输出：SRT（带时间戳），作为 run_long_video 的 secondary 源。
"""
import argparse
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("out_srt")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default=None,
                        help="en/ja/ko；缺省自动检测")
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    t0 = time.time()
    print(f"[whisper] loading model {args.model} ...", flush=True)
    model = WhisperModel(args.model, device="cpu",
                         compute_type="int8")
    print(f"[whisper] model loaded in {time.time()-t0:.0f}s, transcribing ...",
          flush=True)
    segments, info = model.transcribe(
        args.audio,
        language=args.language,
        vad_filter=False,
        beam_size=5,
    )
    print(f"[whisper] detected language={info.language} "
          f"probability={info.language_probability:.2f}", flush=True)

    lines = []
    idx = 1
    for seg in segments:
        start = _fmt(seg.start)
        end = _fmt(seg.end)
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
        idx += 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out_srt)),
                exist_ok=True)
    with open(args.out_srt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[whisper] done: {idx-1} cues -> {args.out_srt} "
          f"({time.time()-t0:.0f}s)", flush=True)
    return 0


def _fmt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


if __name__ == "__main__":
    sys.exit(main())
