#!/usr/bin/env python3
"""Demo JA run: 6YmXji_yUZo (3.1 Story 感想, never used for dev)."""
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("API_KEY") or "")
if not api_key:
    print("[blocked] no API key")
    sys.exit(1)

primary = BASE / "download" / "finalblind-probe" / "6YmXji_yUZo.ja.srt"
empty = BASE / "download" / "_empty_secondary.srt"
if not empty.is_file():
    empty.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")

from pipeline.long_video import run_long_video

out_dir = BASE / "download" / "demo-ja-6YmXji_yUZo"
out_dir.mkdir(exist_ok=True)
final_path = out_dir / "final.zh.srt"
t0 = time.time()
code = run_long_video(
    str(primary), str(empty), str(final_path),
    model="deepseek-v4-flash",
    api_key=api_key,
    batch_size=28,
    game="wuwa",
    resume=False,
    base_url="https://opencode.ai/zen/go/v1",
    proxy="http://127.0.0.1:7890",
    round2_model="deepseek-v4-flash",
    source_language="ja",
)
print(f"exit={code} elapsed={time.time()-t0:.0f}s")
sys.exit(0 if code in (0, None) else 1)
