import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_real_stream_tool_reads_api_key_from_environment():
    source = (ROOT / "tools" / "stream_real_test.py").read_text(encoding="utf-8")

    assert 'os.environ.get("LLM_API_KEY", "")' in source
    assert re.search(
        r"API_KEY\s*=\s*['\"]sk-[A-Za-z0-9_-]{20,}",
        source,
    ) is None


def test_local_secret_and_backup_files_are_ignored():
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "youtube_cookies.txt" in ignore_rules
    assert "*.bak-*" in ignore_rules
    assert "reports/realflow_*/human_review_batch_*.json" in ignore_rules
    assert "reports/realflow_*/human_review_batch_*.csv" in ignore_rules
    assert "reports/realflow_*/human_review_batch_*.audit.jsonl" in ignore_rules
    assert "reports/realflow_*/human_review_metrics_*.json" in ignore_rules
    assert "reports/realflow_*/human_review_metrics_*.md" in ignore_rules


def test_tracked_tree_has_no_personal_home_paths_or_emails():
    excluded = {
        ROOT / "reports" / "asr_candidate_corrections.json",
        ROOT / "reports" / "asr_candidates_auto_ja.json",
    }
    patterns = (
        re.compile(
            r"(?i)[A-Z]:[\\/]+Use" + r"rs[\\/]+[^\\/\s\"'<>]+"
        ),
        re.compile(r"(?i)/Use" + r"rs/[^/\s\"'<>]+"),
        re.compile(r"(?i)/ho" + r"me/[^/\s\"'<>]+"),
        re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"),
    )
    hits = []
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        pytest.skip("tracked-tree privacy check requires a Git checkout")
    for relative in listed.stdout.decode("utf-8").split("\0"):
        if not relative:
            continue
        path = ROOT / relative
        if path in excluded or path.stat().st_size > 10 * 1024 * 1024:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(content) for pattern in patterns):
            hits.append(path.relative_to(ROOT).as_posix())

    assert hits == []
