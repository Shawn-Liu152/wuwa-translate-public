from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_ci_runs_static_checks_and_full_isolated_pytest():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "windows-latest" in workflow
    assert "node --check web/static/app.js" in workflow
    assert "compileall" in workflow
    assert "git diff --check" in workflow
    assert "pytest -q" in workflow
    assert "PYTHONPATH" in workflow


def test_cc_layout_repair_script_is_safe_and_reproducible():
    script = (ROOT / "tools" / "setup_cc_layout.ps1").read_text(encoding="utf-8")

    assert "[ValidateScript" in script
    assert "程序文件" in script
    assert "启动工作台.bat" in script
    assert "停止工作台.bat" in script
    assert "下载内容" in script
    assert "New-Item" in script and "Junction" in script
    assert "Remove-Item -Recurse" not in script
    assert "Resolve-Path" in script


def test_job_storage_helpers_are_split_from_the_job_orchestrator():
    storage = (ROOT / "web" / "job_storage.py").read_text(encoding="utf-8")
    jobs = (ROOT / "web" / "jobs.py").read_text(encoding="utf-8")

    assert "def atomic_json" in storage
    assert "def now" in storage
    assert "def parse_activity_timestamp" in storage
    assert "from web.job_storage import" in jobs
    assert "def atomic_json" not in jobs


def test_current_context_and_handoff_are_compact_and_not_stale():
    import pytest
    pytest.skip("Private handoff and historical context are not distributed")
    context = (ROOT / "docs" / "CODEX_CONTEXT.md").read_text(encoding="utf-8")
    handoff = ROOT / "docs" / "HANDOFF_NEXT_WINDOW.md"
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    ux_contract = (ROOT / "docs" / "tasks" / "UXC-contract.md").read_text(
        encoding="utf-8"
    )

    assert "更新：2026-09-11" in context
    assert "713 passed" not in context
    assert "用户钦定不换模型" not in context
    assert handoff.stat().st_size < 50_000
    assert (ROOT / "docs" / "archive" / "HANDOFF_HISTORY_TO_20260911.md").is_file()
    assert "归档" in agents
    assert "按日期/轮次追加，保留历史" not in agents
    assert "新任务不再生成 waiting_capcut" in ux_contract
