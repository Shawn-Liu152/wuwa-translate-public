import json
import os
import subprocess
import sys
from pathlib import Path


def test_blind_video_runner_accepts_an_arbitrary_single_source_job(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    source = project_root / "tests" / "fixtures" / "ja" / "reaction.ja.srt"
    out_dir = tmp_path / "blind-custom-video"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    proc = subprocess.run(
        [
            sys.executable,
            str(project_root / "tools" / "run_blind_video.py"),
            "ja",
            "custom-video",
            str(source),
            "--out-dir",
            str(out_dir),
            "--dry-run",
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads(
        (out_dir / "holdout_summary.json").read_text(encoding="utf-8")
    )
    assert summary == {
        "lang": "ja",
        "video_id": "custom-video",
        "exit": 0,
        "blind": True,
        "mode": "single-source(empty secondary)",
        "primary": str(source.resolve()),
        "adaptations": [],
    }
    assert (out_dir / "final.zh.srt").is_file()
