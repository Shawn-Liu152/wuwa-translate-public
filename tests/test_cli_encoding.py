"""CLI output remains usable when Windows redirects stdout with a legacy codec."""

import os
import subprocess
import sys
from pathlib import Path


def test_dry_run_report_on_legacy_stdout_encoding(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source.srt"
    output = tmp_path / "result.zh.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0")

    result = subprocess.run(
        [sys.executable, "-m", "pipeline.main", str(source),
         "-o", str(output), "--dry-run", "--fresh"],
        cwd=project_root,
        env=env,
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    assert output.is_file() and output.stat().st_size > 0
    assert b"\\u6027\\u80fd\\u62a5\\u544a" in result.stdout
