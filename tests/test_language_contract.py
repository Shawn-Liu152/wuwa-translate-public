import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.languages import normalize_language_pair
from pipeline.pipeline_manifest import create_manifest, validate_resume
from web import jobs as jobs_module


def test_language_pair_defaults_old_records_to_english_and_simplified_chinese():
    assert normalize_language_pair({}) == {
        "source_language": "en",
        "target_language": "zh-CN",
    }


def test_language_pair_accepts_korean_to_simplified_chinese():
    assert normalize_language_pair({"source_language": "ko"}) == {
        "source_language": "ko",
        "target_language": "zh-CN",
    }


@pytest.mark.parametrize("options", [
    {"source_language": "fr"},
    {"source_language": "en", "target_language": "zh-TW"},
    {"source_language": "", "target_language": "zh-CN"},
])
def test_language_pair_rejects_unsupported_languages(options):
    with pytest.raises(ValueError):
        normalize_language_pair(options)


def test_old_job_without_language_fields_is_loaded_as_english(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", jobs_root)
    manager = jobs_module.JobManager()
    try:
        job_id = "legacyjob"
        workspace = jobs_root / job_id
        workspace.mkdir()
        (workspace / "job.json").write_text(json.dumps({
            "id": job_id,
            "options": {"model": "test", "game": "wuwa"},
        }), encoding="utf-8")

        loaded = manager.get(job_id)

        assert loaded["options"]["source_language"] == "en"
        assert loaded["options"]["target_language"] == "zh-CN"
    finally:
        manager.pool.shutdown(wait=True)


def test_manifest_resume_rejects_a_changed_source_language(tmp_path):
    source = tmp_path / "input.srt"
    source.write_text("input", encoding="utf-8")
    options = {
        "model": "test", "batch_size": 1, "game": "wuwa",
        "source_language": "en", "target_language": "zh-CN",
    }
    manifest = create_manifest(str(source), options, [])

    with pytest.raises(ValueError, match="翻译参数"):
        validate_resume(
            manifest,
            str(source),
            {**options, "source_language": "ja"},
        )


def test_legacy_manifest_without_languages_resumes_as_english(tmp_path):
    source = tmp_path / "input.srt"
    source.write_text("input", encoding="utf-8")
    legacy_options = {"model": "test", "batch_size": 1, "game": "wuwa"}
    manifest = create_manifest(str(source), legacy_options, [])
    manifest["options"].pop("source_language", None)
    manifest["options"].pop("target_language", None)

    validate_resume(
        manifest,
        str(source),
        {
            **legacy_options,
            "source_language": "en",
            "target_language": "zh-CN",
        },
    )


def test_main_handoff_has_one_current_language_contract():
    handoff = (
        Path(__file__).resolve().parents[1]
        / "docs" / "HANDOFF_NEXT_WINDOW.md"
    ).read_text(encoding="utf-8")

    assert "en → zh-CN" in handoff
    assert "ja → zh-CN" in handoff
    assert "ko → zh-CN" in handoff
    assert "当前只开放英语" not in handoff
    assert "268 passed" not in handoff
    assert "dry-run 编排验证" in handoff


def test_main_cli_accepts_korean_source_language(tmp_path):
    """CLI 语言契约：--source-language ko 必须被接受并能跑通 dry-run。"""
    project_root = Path(__file__).resolve().parents[1]
    source = project_root / "tests" / "fixtures" / "ko" / "reaction.ko.srt"
    output = tmp_path / "ko_cli_dry.srt"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    proc = subprocess.run(
        [
            sys.executable, "-m", "pipeline.main",
            str(source),
            "-o", str(output),
            "--source-language", "ko",
            "--dry-run",
        ],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode == 0, (
        f"CLI 拒绝 ko：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert output.exists(), "dry-run 未产出 SRT"
    assert output.stat().st_size > 0
