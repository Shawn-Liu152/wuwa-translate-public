import json
import subprocess
import sys
from pathlib import Path


def test_blind_audit_reports_timeline_residue_duplicates_and_contract_issues(
        tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    final = tmp_path / "final.zh.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n版本3.\n\n"
        "2\n00:00:00,900 --> 00:00:02,000\n1正式上线。\n\n"
        "3\n00:00:02,100 --> 00:00:03,000\nテスト\n\n"
        "4\n00:00:03,100 --> 00:00:04,000\n重复句\n\n"
        "5\n00:00:04,100 --> 00:00:05,000\n重复句\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(project_root / "tools" / "audit_blind_video.py"),
            "ja",
            str(final),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["cues"] == 5
    assert report["overlap"] == 1
    assert report["source_residual"] == 1
    assert report["exact_duplicate_groups"] == 1
    assert report["trailing_full_stop"] == 2
    assert report["split_version_numbers"] == ["3.1"]
