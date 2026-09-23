import errno
import hashlib
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pipeline import long_video
from web import jobs as jobs_module
from web import downloads as downloads_module
from pipeline import source_quality
from pipeline.parser.srt_parser import Subtitle, parse_srt, save_srt
from pipeline.translate.llm import (
    LLMAuthenticationError, LLMConfigurationError, LLMQuotaError,
    LLMTransientError,
)
from web.private_cookie_store import PrivateCookieStore


@pytest.fixture
def manager(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)
    monkeypatch.setattr(
        jobs_module, "TRANSLATION_MEMORY_PATH",
        tmp_path / "translation-memory.json",
    )
    instance = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    yield instance
    instance.close()


def _write_job(manager, tmp_path, *, status="running", generation=3, dry_run=True):
    job_id = "job123"
    workspace = tmp_path / "jobs" / job_id
    workspace.mkdir(parents=True)
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    capcut = workspace / "capcut.en.srt"
    whisper = workspace / "whisper.en.srt"
    capcut.write_text(srt, encoding="utf-8")
    whisper.write_text(srt, encoding="utf-8")
    job = {
        "id": job_id,
        "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(),
        "status": status,
        "stage": "Test",
        "progress": {"completed": 0, "total": 0},
        "inputs": {"capcut_en": str(capcut), "whisper_en": str(whisper)},
        "options": {
            "dry_run": dry_run, "model": "test",
            "base_url": "" if dry_run else "https://api.example.test/v1",
            "batch_size": 1, "game": "wuwa", "local_whisper": False,
        },
        "generation": generation,
        "workspace": str(workspace),
        "artifacts": {},
        "logs": [],
        "error": None,
    }
    manager._save(job)
    return job


def test_direct_create_stops_at_ready_for_translation(manager, tmp_path, monkeypatch):
    capcut = tmp_path / "capcut.srt"
    whisper = tmp_path / "whisper.srt"
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    capcut.write_text(srt, encoding="utf-8")
    whisper.write_text(srt, encoding="utf-8")
    submissions = []
    monkeypatch.setattr(manager, "_submit", lambda *args: submissions.append(args))

    job = manager.create({"capcut_en": capcut, "whisper_en": whisper})

    assert job["status"] == "ready_for_translation"
    assert job["options"]["model"] == ""
    assert job["options"]["api_key_configured"] is False
    assert submissions == []


def test_direct_create_accepts_uploads_from_another_volume(manager, tmp_path, monkeypatch):
    upload_dir = tmp_path / "system-temp"
    upload_dir.mkdir()
    srt = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    capcut = upload_dir / "capcut.srt"
    whisper = upload_dir / "whisper.srt"
    capcut.write_bytes(srt)
    whisper.write_bytes(srt)
    original_replace = Path.replace

    def cross_volume_replace(source, destination):
        if source.parent == upload_dir:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", cross_volume_replace)

    job = manager.create({"capcut_en": capcut, "whisper_en": whisper})

    workspace = tmp_path / "jobs" / job["id"]
    assert (workspace / "capcut.en.srt").read_bytes() == srt
    assert (workspace / "whisper.en.srt").read_bytes() == srt
    assert not capcut.exists() and not whisper.exists()
    assert sorted(path.name for path in workspace.iterdir()) == [
        "capcut.en.srt", "job.json", "whisper.en.srt",
    ]


def test_job_change_snapshot_is_minimal_and_changes_with_progress(
        manager, tmp_path):
    job = _write_job(manager, tmp_path)
    job["options"]["api_key"] = "secret-value"
    job["error"] = "private upstream response"
    job["logs"] = [{
        "seq": 1, "at": jobs_module.now(), "kind": "error",
        "message": "private log text",
    }]
    job["next_event_seq"] = 2
    manager._save(job)

    before = manager.job_change_snapshot()

    assert set(before) == {job["id"]}
    assert set(before[job["id"]]) == {"generation", "updated_at", "revision"}
    serialized = json.dumps(before)
    assert "secret-value" not in serialized
    assert "private upstream response" not in serialized
    assert "private log text" not in serialized

    job["progress"] = {"completed": 1, "total": 3}
    manager._save(job)
    after = manager.job_change_snapshot()

    assert after[job["id"]]["revision"] != before[job["id"]]["revision"]


def test_stage_checkpoint_reuses_only_verified_artifacts(manager, tmp_path):
    job = _write_job(manager, tmp_path)
    artifact = tmp_path / "jobs" / job["id"] / "downloaded.en.srt"
    artifact.write_text("subtitle evidence", encoding="utf-8")
    config = {"url": "https://video.invalid/watch/1", "quality": "1080"}

    manager._record_stage_checkpoint(
        job["id"], job["generation"], "download", config,
        {"youtube_source": str(artifact)},
    )

    reused = manager._reuse_stage_checkpoint(job["id"], "download", config)
    assert reused == {"youtube_source": str(artifact.resolve())}
    assert "_stage_checkpoints" not in manager.public_job(manager.get(job["id"]))

    artifact.write_text("changed evidence", encoding="utf-8")
    assert manager._reuse_stage_checkpoint(job["id"], "download", config) is None
    assert manager._reuse_stage_checkpoint(
        job["id"], "download", {**config, "quality": "720"},
    ) is None


def test_direct_japanese_upload_rejects_english_evidence_before_moving_files(
        manager, tmp_path):
    capcut = tmp_path / "capcut.srt"
    whisper = tmp_path / "whisper.srt"
    srt = (
        "1\n00:00:00,000 --> 00:00:01,000\nHello there\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nThis is English\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\nDo not label it Japanese\n"
    )
    capcut.write_text(srt, encoding="utf-8")
    whisper.write_text(srt, encoding="utf-8")

    with pytest.raises(ValueError, match="源语言|日语"):
        manager.create(
            {"capcut_en": capcut, "whisper_en": whisper},
            options={"source_language": "ja", "target_language": "zh-CN"},
        )

    assert capcut.is_file()
    assert whisper.is_file()


@pytest.mark.parametrize("legacy_workflow_mode", ["quick", "quality"])
def test_new_url_task_never_waits_for_capcut(
        manager, monkeypatch, legacy_workflow_mode):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    def fake_download(url, output_dir, **kwargs):
        output = Path(output_dir)
        subtitle = output / "youtube.en.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
            encoding="utf-8",
        )
        return {"youtube_en": str(subtitle)}

    class GoodQuality:
        usable = True

        def to_dict(self):
            return {"usable": True, "score": 100}

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(source_quality, "assess_english_srt", lambda *_: GoodQuality())
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": legacy_workflow_mode,
            "reference_strategy": "adaptive",
            "download_video": True,
            "download_subtitles": True,
            "download_thumbnail": False,
        },
    )

    manager._run_download(job["id"], "", 0)
    current = manager.get(job["id"])
    assert current["status"] == "ready_for_translation"
    assert current["stage"] == "Sources ready"
    assert current["options"]["proxy"] == "http://127.0.0.1:7890"
    assert current["inputs"]["capcut_en"] != current["inputs"]["whisper_en"]
    assert Path(current["inputs"]["capcut_en"]).name == "youtube.en.srt"
    assert Path(current["inputs"]["whisper_en"]).read_text(encoding="utf-8") == ""


def test_legacy_waiting_capcut_task_can_continue_with_automatic_source(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="waiting_capcut")
    workspace = Path(job["workspace"])
    automatic = workspace / "youtube.en.srt"
    automatic.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
        encoding="utf-8",
    )
    job["inputs"] = {}
    job["artifacts"] = {"youtube_source": str(automatic)}
    job["reference_source"] = "youtube"
    job["source_qualities"] = {"youtube": {"score": 100, "usable": True}}
    manager._save(job)

    continued = manager.continue_with_automatic_source(job["id"])

    assert continued["status"] == "ready_for_translation"
    assert Path(continued["inputs"]["primary_srt"]).name == "youtube.en.srt"
    assert Path(continued["inputs"]["secondary_srt"]).read_text(
        encoding="utf-8"
    ) == ""


def test_download_retry_reuses_a_verified_download_checkpoint(
        manager, monkeypatch):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "youtube",
            "download_video": False,
            "download_subtitles": True,
            "download_thumbnail": False,
        },
    )
    workspace = Path(job["workspace"])
    subtitle = workspace / "youtube.en.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
        encoding="utf-8",
    )
    manager._record_stage_checkpoint(
        job["id"], 0, "download", manager._download_stage_config(job),
        {"youtube_source": str(subtitle), "youtube_en": str(subtitle)},
    )

    def unexpected_download(*args, **kwargs):
        raise AssertionError("verified download checkpoint must skip yt-dlp")

    class GoodQuality:
        usable = True

        def to_dict(self):
            return {"usable": True, "score": 100}

    monkeypatch.setattr(downloads_module, "run_download", unexpected_download)
    monkeypatch.setattr(source_quality, "assess_english_srt", lambda *_: GoodQuality())

    manager._run_download(job["id"], "", 0)

    current = manager.get(job["id"])
    assert current["status"] == "ready_for_translation"
    assert any(
        "复用已校验的下载阶段产物" in event["message"]
        for event in current["logs"]
    )


@pytest.mark.parametrize("source_language", ["ja", "ko"])
def test_multilingual_quality_mode_does_not_require_capcut(
        manager, monkeypatch, source_language):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    def fake_download(url, output_dir, **kwargs):
        output = Path(output_dir)
        subtitle = output / f"youtube.{source_language}.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nsource text\n",
            encoding="utf-8",
        )
        return {"youtube_source": str(subtitle)}

    class GoodQuality:
        usable = True
        needs_secondary = False

        def to_dict(self):
            return {"usable": True, "score": 95, "language_ratio": 1.0}

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(source_quality, "assess_source_srt", lambda *a, **k: GoodQuality())
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "source_language": source_language,
            "workflow_mode": "quality",
            "reference_strategy": "adaptive",
            "download_video": False,
            "download_subtitles": True,
            "download_thumbnail": False,
        },
    )

    manager._run_download(job["id"], "", 0)
    current = manager.get(job["id"])

    assert current["status"] == "ready_for_translation"
    assert current["source_selection"]["primary_text"] == "youtube"
    assert Path(current["inputs"]["primary_srt"]).name == f"youtube.{source_language}.srt"


def _write_multilingual_ready_job(manager, tmp_path, current_score=95):
    workspace = tmp_path / "jobs" / "ko-ready"
    workspace.mkdir(parents=True)
    primary = workspace / "youtube.ko.srt"
    secondary = workspace / "whisper.ko.srt"
    save_srt(str(primary), [
        Subtitle(i, f"00:00:{i:02d},000", f"00:00:{i:02d},800", f"한국어 원문 {i}")
        for i in range(1, 11)
    ])
    secondary.write_text("", encoding="utf-8")
    job = {
        "id": "ko-ready", "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(), "status": "ready_for_translation",
        "stage": "Sources ready", "generation": 0,
        "workspace": str(workspace), "artifacts": {}, "logs": [],
        "inputs": {"primary_srt": str(primary), "secondary_srt": str(secondary)},
        "options": {"source_language": "ko", "target_language": "zh-CN"},
        "source_qualities": {"youtube": {"score": current_score, "usable": True}},
        "source_selection": {"primary_text": "youtube"},
        "error": None,
    }
    manager._save(job)
    return job, primary


def test_low_quality_korean_capcut_cannot_replace_better_automatic_source(
        manager, tmp_path):
    job, primary = _write_multilingual_ready_job(manager, tmp_path, current_score=95)
    capcut = tmp_path / "capcut-low.ko.srt"
    save_srt(str(capcut), [
        Subtitle(i, f"00:00:{i // 2:02d},{(i % 2) * 500:03d}",
                 f"00:00:{i // 2:02d},{(i % 2) * 500 + 400:03d}", "같은 자막입니다")
        for i in range(1, 11)
    ])

    updated = manager.attach_capcut(job["id"], capcut)

    assert Path(updated["inputs"]["primary_srt"]) == primary
    assert updated["source_selection"]["primary_text"] == "youtube"


def test_high_quality_korean_capcut_can_win_automatic_selection(manager, tmp_path):
    job, primary = _write_multilingual_ready_job(manager, tmp_path, current_score=40)
    capcut = tmp_path / "capcut-good.ko.srt"
    save_srt(str(capcut), [
        Subtitle(i, f"00:00:{i:02d},000", f"00:00:{i:02d},800", f"서로 다른 완전한 문장 {i}입니다.")
        for i in range(1, 11)
    ])

    updated = manager.attach_capcut(job["id"], capcut)

    assert Path(updated["inputs"]["primary_srt"]).name == "capcut.ko.srt"
    assert Path(updated["inputs"]["secondary_srt"]) == primary
    assert updated["source_selection"]["primary_text"] == "capcut"


def _write_completed_url_job(manager, tmp_path, status="completed"):
    """只下字幕的 URL 任务走完流程后的形态：youtube 主源 + 空第二源。"""
    job_id = "completed-url"
    workspace = tmp_path / "jobs" / job_id
    workspace.mkdir(parents=True)
    youtube = workspace / "youtube.en.srt"
    secondary = workspace / "reference.secondary.empty.en.srt"
    youtube.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello there\n"
        "2\n00:00:02,000 --> 00:00:04,000\nGeneral Kenobi\n",
        encoding="utf-8",
    )
    secondary.write_text("", encoding="utf-8")
    job = {
        "id": job_id, "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(), "status": status,
        "stage": "Deliverable" if status == "completed" else "API request failed",
        "generation": 2,
        "workspace": str(workspace), "artifacts": {}, "logs": [],
        "url": "https://youtu.be/completedurl",
        "inputs": {
            "primary_srt": str(youtube), "secondary_srt": str(secondary),
        },
        "options": {"source_language": "en", "target_language": "zh-CN"},
        "source_qualities": {"youtube": {"score": 75, "usable": True}},
        "source_selection": {"primary_text": "youtube"},
        "error": None,
    }
    manager._save(job)
    capcut = workspace / "capcut.en.srt"
    capcut.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello there my friend\n"
        "2\n00:00:02,000 --> 00:00:04,000\nA completely different line\n",
        encoding="utf-8",
    )
    return job, capcut


def test_attach_capcut_on_completed_url_job_reopens_translation(manager, tmp_path):
    """P2：只下字幕的 URL 任务完成后也能补第二源证据，任务回 ready 待重翻。"""
    job, capcut = _write_completed_url_job(manager, tmp_path)

    updated = manager.attach_capcut(job["id"], capcut)

    assert updated["status"] == "ready_for_translation"
    assert Path(workspace_capcut(updated)).name == "capcut.en.srt"


def test_attach_capcut_on_failed_url_job_is_allowed(manager, tmp_path):
    """失败任务同样允许补第二源后重新翻译（不要求重建任务）。"""
    job, capcut = _write_completed_url_job(manager, tmp_path, status="failed")

    updated = manager.attach_capcut(job["id"], capcut)

    assert updated["status"] == "ready_for_translation"


def test_attach_capcut_still_rejects_active_jobs(manager, tmp_path):
    """回归护栏：queued/running 任务不允许追加（避免与活动 worker 竞态）。"""
    job, capcut = _write_completed_url_job(manager, tmp_path)
    job["status"] = "running"
    manager._save(job)

    with pytest.raises(ValueError):
        manager.attach_capcut(job["id"], capcut)


def test_start_translation_reanchors_inputs_missing_after_organization(
        manager, tmp_path, monkeypatch):
    """真实 bug（2026-09-06 任务 6311f11a3cad）：完成任务文件收进 过程文件/
    之后，存量 job.json 的 inputs 仍指工作区根目录（存储往返丢失子目录），
    任何"从头重跑/继续失败批次"都会 FileNotFoundError → pipeline_error。
    start_translation 必须在原路径缺失时回锚到 过程文件/ 同名文件。"""
    job_id = "org-heal"
    workspace = tmp_path / "jobs" / job_id
    process_dir = workspace / "过程文件"
    process_dir.mkdir(parents=True)
    primary = process_dir / "youtube.en.srt"
    primary.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello there\n",
        encoding="utf-8",
    )
    secondary = process_dir / "reference.secondary.empty.en.srt"
    secondary.write_text("", encoding="utf-8")
    job = {
        "id": job_id, "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(), "status": "ready_for_translation",
        "stage": "Sources ready", "generation": 1,
        "workspace": str(workspace), "artifacts": {}, "logs": [],
        "url": "https://youtu.be/orgheal",
        # 污染态：指向已不存在的根目录路径
        "inputs": {
            "primary_srt": str(workspace / "youtube.en.srt"),
            "secondary_srt": str(workspace / "reference.secondary.empty.en.srt"),
        },
        "options": {
            "source_language": "en", "target_language": "zh-CN",
            "dry_run": True, "model": "test",
        },
        "source_qualities": {"youtube": {"score": 75, "usable": True}},
        "source_selection": {"primary_text": "youtube"},
        "error": None,
    }
    manager._save(job)
    submissions = []
    monkeypatch.setattr(manager, "_submit", lambda *args: submissions.append(args))

    updated = manager.start_translation(job_id, "")

    inputs = updated["inputs"]
    assert Path(inputs["primary_srt"]).is_file()
    assert Path(inputs["primary_srt"]).parent.name == "过程文件"
    assert Path(inputs["secondary_srt"]).is_file()
    assert submissions, "任务应已提交运行"


def test_resolve_stored_input_prefers_existing_original(manager, tmp_path):
    """回归护栏：原路径仍存在时不得回锚（防误并同名新文件）。"""
    workspace = tmp_path / "jobs" / "keep-origin"
    workspace.mkdir(parents=True)
    original = workspace / "youtube.en.srt"
    original.write_text("1\n00:00:00,000 --> 00:00:01,000\nA\n", encoding="utf-8")
    process_dir = workspace / "过程文件"
    process_dir.mkdir()
    (process_dir / "youtube.en.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nB\n", encoding="utf-8",
    )

    resolved = manager._resolve_stored_input("keep-origin", str(original))

    assert Path(resolved) == original.resolve()


def workspace_capcut(updated_job):
    inputs = updated_job["inputs"]
    value = inputs.get("primary_srt") or inputs.get("capcut_en")
    if Path(value).name != "capcut.en.srt":
        value = inputs.get("secondary_srt") or inputs.get("whisper_en")
    return value


def test_review_edit_rebuilds_display_cues_from_semantic_unit(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    workspace = Path(job["workspace"])
    semantic = workspace / "final.round2.zh.srt"
    final = workspace / "final.zh.srt"
    display_map = workspace / "final.zh.display-map.json"
    save_srt(str(semantic), [Subtitle(
        1, "00:00:00,000", "00:00:12,000", "旧的完整语义译文",
    )])
    save_srt(str(final), [
        Subtitle(1, "00:00:00,000", "00:00:06,000", "旧的完整"),
        Subtitle(2, "00:00:06,000", "00:00:12,000", "语义译文"),
    ])
    display_map.write_text(json.dumps({"1": [
        {"start": "00:00:00,000", "end": "00:00:04,000"},
        {"start": "00:00:04,000", "end": "00:00:08,000"},
        {"start": "00:00:08,000", "end": "00:00:12,000"},
    ]}), encoding="utf-8")
    job["artifacts"].update({
        semantic.name: str(semantic), final.name: str(final),
        display_map.name: str(display_map),
    })
    manager._save(job)

    manager._write_reviewed_srt(job, [{
        "subtitle_id": 1, "state": "resolved",
        "manual_text": "我不知道她想做什么，不过这个决定真的很奇怪。",
    }])

    reviewed = parse_srt(str(workspace / "final.reviewed.zh.srt"))
    assert len(reviewed) >= 2
    assert "".join(item.text for item in reviewed) == "我不知道她想做什么，不过这个决定真的很奇怪。"
    assert reviewed[0].start == "00:00:00,000"
    assert reviewed[-1].end == "00:00:12,000"


def _write_burnable_job(manager, tmp_path):
    job = _write_job(
        manager,
        tmp_path,
        status="completed",
        dry_run=False,
    )
    workspace = Path(job["workspace"])
    video = workspace / "source.mp4"
    final = workspace / "final.zh.srt"
    video.write_bytes(b"video-input")
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好\n",
        encoding="utf-8",
    )
    job.update(
        stage="Deliverable",
        delivery_status="deliverable",
        artifacts={"video": str(video), final.name: str(final)},
    )
    manager._save(job)
    return manager.get(job["id"])


def test_burn_request_is_independent_and_prefers_reviewed_subtitle(
        manager, tmp_path, monkeypatch):
    job = _write_burnable_job(manager, tmp_path)
    reviewed = Path(job["workspace"]) / "final.reviewed.zh.srt"
    reviewed.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n已审校\n",
        encoding="utf-8",
    )
    loaded = manager._load(job["id"])
    loaded["artifacts"][reviewed.name] = str(reviewed)
    manager._save(loaded)
    submissions = []
    monkeypatch.setattr(
        manager,
        "_submit_burn",
        lambda job_id, *args: submissions.append((job_id, *args)),
    )

    requested = manager.request_burned_video(job["id"])

    assert requested["status"] == "completed"
    assert requested["delivery_status"] == "deliverable"
    export = requested["exports"]["burned_video"]
    assert export["requested"] is True
    assert export["status"] == "queued"
    assert export["progress"] == 0
    assert export["artifact"] == "final.zh.burned.mp4"
    assert export["style"] == "black-outline-white-2-v1"
    assert len(submissions) == 1
    assert Path(submissions[0][-1]).name == "final.reviewed.zh.srt"


def test_burn_request_accepts_completed_human_queue_with_auto_resolved_items(
        manager, tmp_path, monkeypatch):
    job = _write_burnable_job(manager, tmp_path)
    workspace = Path(job["workspace"])
    reviewed = workspace / "final.reviewed.zh.srt"
    reviewed.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n已审校\n",
        encoding="utf-8",
    )
    risk_path = workspace / "final.zh.risk-queue.json"
    risks = [
        {
            "key": "automatic",
            "subtitle_id": 1,
            "review_required": True,
            "state": "auto_resolved",
            "translated": "你好",
        },
        {
            "key": "resolved",
            "subtitle_id": 1,
            "review_required": True,
            "state": "resolved",
            "translated": "你好",
            "manual_text": "已审校",
        },
        {
            "key": "verified",
            "subtitle_id": 1,
            "review_required": True,
            "state": "verified",
            "translated": "你好",
        },
    ]
    risk_path.write_text(json.dumps({"items": risks}), encoding="utf-8")
    loaded = manager._load(job["id"])
    loaded["delivery_status"] = "review_required"
    loaded["artifacts"][reviewed.name] = str(reviewed)
    loaded["artifacts"][risk_path.name] = str(risk_path)
    manager._save(loaded)
    submissions = []
    monkeypatch.setattr(
        manager,
        "_submit_burn",
        lambda job_id, *args: submissions.append((job_id, *args)),
    )

    requested = manager.request_burned_video(job["id"])

    assert requested["exports"]["burned_video"]["status"] == "queued"
    assert len(submissions) == 1
    assert Path(submissions[0][-1]).name == "final.reviewed.zh.srt"


def test_burn_request_still_blocks_unfinished_human_review(
        manager, tmp_path):
    job = _write_burnable_job(manager, tmp_path)
    workspace = Path(job["workspace"])
    reviewed = workspace / "final.reviewed.zh.srt"
    reviewed.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n已审校\n",
        encoding="utf-8",
    )
    risk_path = workspace / "final.zh.risk-queue.json"
    risk_path.write_text(
        json.dumps({"items": [{
            "key": "unfinished",
            "subtitle_id": 1,
            "review_required": True,
            "state": "review",
            "translated": "你好",
        }]}),
        encoding="utf-8",
    )
    loaded = manager._load(job["id"])
    loaded["delivery_status"] = "review_required"
    loaded["artifacts"][reviewed.name] = str(reviewed)
    loaded["artifacts"][risk_path.name] = str(risk_path)
    manager._save(loaded)

    with pytest.raises(ValueError, match="审校"):
        manager.request_burned_video(job["id"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"delivery_status": "review_required"}, "审校"),
        ({"options": {"dry_run": True}}, "试运行"),
        ({"artifacts": {}}, "视频"),
    ],
)
def test_burn_request_enforces_delivery_and_input_gates(
        manager, tmp_path, change, message):
    job = _write_burnable_job(manager, tmp_path)
    loaded = manager._load(job["id"])
    for key, value in change.items():
        if key == "options":
            loaded["options"].update(value)
        else:
            loaded[key] = value
    manager._save(loaded)

    with pytest.raises(ValueError, match=message):
        manager.request_burned_video(job["id"])


def test_burn_success_adds_artifact_without_changing_main_job(
        manager, tmp_path, monkeypatch):
    from pipeline import subtitle_burner

    job = _write_burnable_job(manager, tmp_path)
    monkeypatch.setattr(manager, "_submit_burn", lambda *_args: None)
    queued = manager.request_burned_video(job["id"])
    export = queued["exports"]["burned_video"]
    video = Path(queued["artifacts"]["video"])
    subtitle = Path(queued["artifacts"]["final.zh.srt"])

    def fake_burn(request, *, on_progress, cancel_check):
        assert cancel_check() is False
        on_progress(47)
        request.output_path.write_bytes(b"rendered-video")
        return subtitle_burner.BurnResult(
            request.output_path,
            "libx264",
            12.5,
        )

    monkeypatch.setattr(subtitle_burner, "burn_subtitles", fake_burn)
    manager._run_burned_video(
        job["id"],
        export["input_fingerprint"],
        video,
        subtitle,
    )

    completed = manager.get(job["id"])
    assert completed["status"] == "completed"
    assert completed["delivery_status"] == "deliverable"
    assert completed["exports"]["burned_video"]["status"] == "completed"
    assert completed["exports"]["burned_video"]["progress"] == 100
    assert completed["exports"]["burned_video"]["encoder"] == "libx264"
    assert "final.zh.burned.mp4" in completed["artifacts"]
    assert Path(completed["artifacts"]["video"]).read_bytes() == b"video-input"


def test_interrupted_burn_recovers_without_failing_completed_translation(
        manager, tmp_path):
    job = _write_burnable_job(manager, tmp_path)
    loaded = manager._load(job["id"])
    loaded["exports"] = {"burned_video": {
        "requested": True,
        "status": "running",
        "progress": 18,
        "artifact": "final.zh.burned.mp4",
        "style": "black-outline-white-2-v1",
        "error_code": None,
        "message": None,
        "input_fingerprint": "sha256:test",
        "updated_at": jobs_module.now(),
    }}
    manager._save(loaded)

    manager.recover_interrupted_jobs()

    recovered = manager.get(job["id"])
    assert recovered["status"] == "completed"
    assert recovered["delivery_status"] == "deliverable"
    export = recovered["exports"]["burned_video"]
    assert export["status"] == "failed"
    assert export["error_code"] == "burn_interrupted"
    assert "重新生成" in export["message"]


def test_requested_burn_waits_for_review_then_queues_reviewed_subtitles(
        manager, tmp_path, monkeypatch):
    job = _write_burnable_job(manager, tmp_path)
    workspace = Path(job["workspace"])
    risk_path = workspace / "final.zh.risk-queue.json"
    risks = [{
        "key": "risk-1",
        "subtitle_id": 1,
        "review_required": True,
        "state": "review",
        "translated": "你好",
    }]
    risk_path.write_text(json.dumps({"items": risks}), encoding="utf-8")
    loaded = manager._load(job["id"])
    loaded["delivery_status"] = "review_required"
    loaded["options"]["burn_after_translation"] = True
    loaded["artifacts"][risk_path.name] = str(risk_path)
    manager._save(loaded)
    submissions = []
    monkeypatch.setattr(
        manager,
        "_submit_burn",
        lambda job_id, *args: submissions.append((job_id, *args)),
    )

    manager._defer_or_start_requested_burn(job["id"])

    pending = manager.get(job["id"])
    assert pending["exports"]["burned_video"]["status"] == "pending"
    assert "审校" in pending["exports"]["burned_video"]["message"]
    assert submissions == []

    risks[0]["state"] = "verified"
    risk_path.write_text(json.dumps({"items": risks}), encoding="utf-8")
    manager._write_reviewed_srt(manager._load(job["id"]), risks)

    queued = manager.get(job["id"])
    assert queued["exports"]["burned_video"]["status"] == "queued"
    assert Path(submissions[0][-1]).name == "final.reviewed.zh.srt"


def test_japanese_url_job_passes_ja_to_downloader_and_whisper(
        manager, monkeypatch):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)
    observed = {}

    def fake_download(url, output_dir, **kwargs):
        observed["download_language"] = kwargs["source_language"]
        output = Path(output_dir)
        video = output / "video.mp4"
        subtitle = output / "youtube.ja.srt"
        video.write_bytes(b"video")
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
            encoding="utf-8",
        )
        return {"video": str(video), "youtube_source": str(subtitle)}

    class Quality:
        usable = False

        def to_dict(self):
            return {"usable": self.usable, "score": 0}

    def fake_whisper(command, cancel_check=None, emit=None):
        observed["whisper_command"] = command
        output = Path(command[command.index("-o") + 1])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(
        source_quality,
        "assess_source_srt",
        lambda path, **_kwargs: type("SourceQuality", (), {
            "usable": str(path).endswith("reference.whisper.ja.srt"),
            "to_dict": lambda self: {"usable": self.usable, "score": 100 if self.usable else 0},
        })(),
    )
    monkeypatch.setattr(long_video, "_run_cancellable", fake_whisper)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "source_language": "ja", "target_language": "zh-CN",
            "workflow_mode": "quick", "reference_strategy": "whisper",
            "download_video": True, "download_subtitles": True,
            "download_thumbnail": False,
        },
    )

    manager._run_download(job["id"], "", 0)
    current = manager.get(job["id"])

    assert observed["download_language"] == "ja"
    assert observed["whisper_command"][observed["whisper_command"].index("--language") + 1] == "ja"
    assert Path(current["inputs"]["primary_srt"]).name == "reference.whisper.ja.srt"
    assert Path(current["inputs"]["secondary_srt"]).name == "youtube.ja.srt"


def test_url_job_uses_then_deletes_private_cookie_file(
        manager, tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)
    cookie_source = tmp_path / "cookies.txt"
    cookie_source.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate\n",
        encoding="utf-8",
    )
    observed_cookie = None

    def fake_download(url, output_dir, **kwargs):
        nonlocal observed_cookie
        observed_cookie = Path(kwargs["cookies_file"])
        assert observed_cookie.is_file()
        assert kwargs["cookies_from_browser"] == ""
        output = Path(output_dir)
        subtitle = output / "youtube.en.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
            encoding="utf-8",
        )
        return {"youtube_en": str(subtitle)}

    class GoodQuality:
        usable = True

        def to_dict(self):
            return {"usable": True, "score": 100}

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(source_quality, "assess_english_srt", lambda *_: GoodQuality())
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "adaptive",
            "download_video": True,
            "download_subtitles": True,
            "download_thumbnail": False,
            "youtube_auth": "file",
        },
        youtube_cookie=cookie_source.read_bytes(),
    )

    manager._run_download(job["id"], "", 0)

    assert observed_cookie is not None
    assert not observed_cookie.exists()
    assert manager.get(job["id"])["status"] == "ready_for_translation"


def test_whisper_strategy_keeps_youtube_as_independent_secondary_source(
        manager, monkeypatch):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)
    observed_download_options = {}

    def fake_download(url, output_dir, **kwargs):
        observed_download_options.update(kwargs)
        output = Path(output_dir)
        video = output / "video.mp4"
        youtube = output / "youtube.en.srt"
        video.write_bytes(b"video")
        youtube.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nYouTube evidence\n",
            encoding="utf-8",
        )
        return {"video": str(video), "youtube_en": str(youtube)}

    class Quality:
        usable = True

        def to_dict(self):
            return {"usable": True, "score": 100}

    def fake_whisper(command, cancel_check=None, emit=None):
        output = Path(command[command.index("-o") + 1])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nWhisper evidence\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(source_quality, "assess_english_srt", lambda *_: Quality())
    monkeypatch.setattr(long_video, "_run_cancellable", fake_whisper)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "whisper",
            "download_video": True,
            "download_subtitles": True,
            "download_thumbnail": False,
        },
    )

    manager._run_download(job["id"], "", 0)

    current = manager.get(job["id"])
    assert observed_download_options["download_subtitles"] is True
    assert Path(current["inputs"]["capcut_en"]).name == "reference.whisper.en.srt"
    assert Path(current["inputs"]["whisper_en"]).name == "youtube.en.srt"
    assert current["inputs"]["capcut_en"] != current["inputs"]["whisper_en"]


def test_adaptive_mode_keeps_youtube_text_on_whisper_timeline(
        manager, monkeypatch):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    def fake_download(url, output_dir, **kwargs):
        output = Path(output_dir)
        video = output / "video.mp4"
        youtube = output / "youtube.en.srt"
        video.write_bytes(b"video")
        youtube.write_text(
            "1\n00:00:02,230 --> 00:00:03,919\n"
            "I'm Corey, a video game composer, and today we're\n\n"
            "2\n00:00:03,919 --> 00:00:11,509\n"
            "going to listen to more music from Wuthering Waves.\n",
            encoding="utf-8",
        )
        return {"video": str(video), "youtube_en": str(youtube)}

    class Quality:
        usable = True
        needs_secondary = True
        needs_timing_alignment = True

        def to_dict(self):
            return {
                "usable": True,
                "score": 100,
                "needs_secondary": True,
                "needs_timing_alignment": True,
            }

    def fake_whisper(command, cancel_check=None, emit=None):
        output = Path(command[command.index("-o") + 1])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:06,540\n"
            "I'm Corey, a video game composer, and today we're going to "
            "listen to more music from Wuthering Waves.\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(source_quality, "assess_english_srt", lambda *_: Quality())
    monkeypatch.setattr(long_video, "_run_cancellable", fake_whisper)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "adaptive",
            "download_video": True,
            "download_subtitles": True,
            "download_thumbnail": False,
        },
    )

    manager._run_download(job["id"], "", 0)

    current = manager.get(job["id"])
    primary = Path(current["inputs"]["capcut_en"])
    assert primary.name == "youtube.aligned.en.srt"
    assert Path(current["inputs"]["whisper_en"]).name == "reference.whisper.en.srt"
    aligned = parse_srt(str(primary))
    assert aligned[0].text.startswith("I'm Corey")
    assert aligned[0].start == "00:00:00,000"
    assert aligned[1].end == "00:00:06,540"


@pytest.mark.parametrize(
    "source_language,expected_model,expects_no_vad",
    [("en", "distil-large-v3", True), ("ja", "turbo", False), ("ko", "turbo", False)],
)
def test_web_whisper_reference_uses_language_specific_model_and_vad(
        manager, monkeypatch, source_language, expected_model, expects_no_vad):
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    def fake_download(url, output_dir, **kwargs):
        output = Path(output_dir)
        video = output / "video.mp4"
        video.write_bytes(b"video")
        return {"video": str(video)}

    class Quality:
        def __init__(self, usable):
            self.usable = usable

        def to_dict(self):
            return {"usable": self.usable, "score": 100 if self.usable else 0}

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(
        source_quality, "assess_english_srt",
        lambda path: Quality(bool(path and Path(path).exists())),
    )
    monkeypatch.setattr(
        source_quality, "assess_source_srt",
        lambda path, source_language: Quality(bool(path and Path(path).exists())),
    )

    def fake_whisper(command, cancel_check=None, emit=None):
        assert command[command.index("--model") + 1] == expected_model
        assert ("--no-vad" in command) is expects_no_vad
        assert command[command.index("--language") + 1] == source_language
        output = Path(command[command.index("-o") + 1])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(long_video, "_run_cancellable", fake_whisper)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "whisper",
            "download_video": True,
            "download_subtitles": False,
            "download_thumbnail": False,
            "source_language": source_language,
        },
    )

    manager._run_download(job["id"], "", 0)

    current = manager.get(job["id"])
    assert current["status"] == "ready_for_translation"
    assert current["reference_source"] == "whisper"


def test_start_translation_maps_downloaded_video_for_local_whisper(
        manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="ready_for_translation")
    video = Path(job["workspace"]) / "downloaded.mp4"
    video.write_bytes(b"video")
    job["artifacts"]["video"] = str(video)
    manager._save(job)
    submissions = []
    monkeypatch.setattr(manager, "_submit", lambda *args: submissions.append(args))

    started = manager.start_translation(
        job["id"], "", translation_options={
            "model": "test", "batch_size": 20, "game": "wuwa",
            "local_whisper": True, "dry_run": True,
        },
    )

    assert started["status"] == "queued"
    assert started["inputs"]["video"] == str(video)
    assert started["options"]["local_whisper"] is True
    assert len(submissions) == 1


def test_formal_start_requires_endpoint_before_mutating_job(
        manager, tmp_path, monkeypatch):
    job = _write_job(
        manager, tmp_path, status="ready_for_translation", dry_run=False,
    )
    job["options"]["base_url"] = ""
    manager._save(job)
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    with pytest.raises(ValueError, match="API 接口地址不能为空"):
        manager.start_translation(
            job["id"], "top-secret",
            translation_options={"model": "test", "dry_run": False},
        )

    current = manager.get(job["id"])
    assert current["status"] == "ready_for_translation"
    assert current["generation"] == job["generation"]
    assert job["id"] not in manager.secrets


def test_job_manager_rejects_remote_plain_http_endpoint(
        manager, tmp_path, monkeypatch):
    job = _write_job(
        manager, tmp_path, status="ready_for_translation", dry_run=False,
    )
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    with pytest.raises(ValueError, match="远程 API 接口必须使用 HTTPS"):
        manager.start_translation(
            job["id"], "top-secret",
            translation_options={
                "model": "test", "dry_run": False,
                "base_url": "http://api.example.com/v1",
            },
        )

    assert manager.get(job["id"])["status"] == "ready_for_translation"
    assert job["id"] not in manager.secrets


def test_pause_and_resume_waiting_for_capcut_without_losing_the_task(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="waiting_capcut")
    job["stage"] = "Waiting for CapCut English SRT"
    manager._save(job)

    paused = manager.pause(job["id"])

    assert paused["status"] == "paused"
    assert paused["paused_from"] == "waiting_capcut"
    assert paused["generation"] == job["generation"] + 1

    resumed = manager.resume(job["id"])

    assert resumed["status"] == "waiting_capcut"
    assert resumed["stage"] == "Waiting for CapCut English SRT"
    assert "paused_from" not in resumed


def test_waiting_for_capcut_task_can_be_deleted_immediately(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="waiting_capcut")

    assert manager.delete(job["id"]) == {"deleted": job["id"]}
    assert not Path(job["workspace"]).exists()


def test_resume_paused_translation_reuses_manifest_path(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="running", dry_run=True)
    submissions = []
    monkeypatch.setattr(manager, "_submit", lambda *args: submissions.append(args))

    manager.pause(job["id"])
    resumed = manager.resume(job["id"])

    assert resumed["status"] == "queued"
    assert resumed["resume"] is True
    assert submissions[0][1] == manager._run


@pytest.mark.parametrize("action, terminal", [("force_reset", "failed"), ("cancel", "cancelled")])
def test_old_worker_cannot_overwrite_terminal_state(manager, tmp_path, action, terminal):
    job = _write_job(manager, tmp_path)

    getattr(manager, action)(job["id"])

    assert manager._update_if_current(
        job["id"], job["generation"], status="completed", stage="Completed"
    ) is None
    assert not manager._event_if_current(job["id"], job["generation"], "info", "late")
    current = manager.get(job["id"])
    assert current["status"] == terminal
    assert current["generation"] == job["generation"] + 1


def test_delivery_status_distinguishes_review_and_unreliable_outputs():
    assert jobs_module._delivery_status({"quality": {
        "review_required_count": 0, "risk_rate": 0.1,
        "review_required_rate": 0.0,
    }}) == "deliverable"
    assert jobs_module._delivery_status({"quality": {
        "review_required_count": 2, "risk_rate": 0.3,
        "review_required_rate": 0.2,
    }}) == "review_required"
    assert jobs_module._delivery_status({"quality": {
        "review_required_count": 9, "risk_rate": 0.9,
        "review_required_rate": 0.7,
    }}) == "source_unreliable"


def test_restart_removes_manifest_without_path_type_error(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="failed")
    manifest = tmp_path / "jobs" / job["id"] / "final.zh.srt.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        manager, "start_translation",
        lambda job_id, secret, resume=False: manager.get(job_id),
    )

    retried = manager.retry_failed(job["id"], "", restart=True)

    assert retried["status"] == "ready_for_translation"
    assert not manifest.exists()
    assert any("manifest" in event["message"] for event in retried["logs"])


def test_retry_keeps_manifest_and_resumes(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="failed")
    manifest = tmp_path / "jobs" / job["id"] / "final.zh.srt.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    calls = []

    def start(job_id, secret, resume=False):
        calls.append(resume)
        return manager.get(job_id)

    monkeypatch.setattr(manager, "start_translation", start)
    manager.retry_failed(job["id"], "")

    assert manifest.exists()
    assert calls == [True]


def test_concurrent_retry_submits_one_new_generation(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="failed")
    submissions = []
    monkeypatch.setattr(manager.pool, "submit", lambda *args: submissions.append(args))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(manager.retry_failed, job["id"], "") for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ValueError:
                outcomes.append(None)

    current = manager.get(job["id"])
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert current["generation"] == job["generation"] + 1
    assert current["status"] == "queued"
    assert len(submissions) == 1


def test_event_and_update_preserve_logs_and_state(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="queued")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(manager._event, job["id"], "info", f"log-{i}") for i in range(50)]
        futures += [pool.submit(manager._update, job["id"], stage=f"stage-{i}") for i in range(50)]
        for future in futures:
            future.result()

    current = manager.get(job["id"])
    assert len(current["logs"]) == 50
    assert {event["message"] for event in current["logs"]} == {f"log-{i}" for i in range(50)}
    assert current["stage"].startswith("stage-")
    assert [event["seq"] for event in current["logs"]] == list(range(1, 51))
    json.loads(manager._path(job["id"]).read_text(encoding="utf-8"))


def test_event_sequence_survives_log_window_truncation():
    job = {"logs": [], "next_event_seq": 1}
    for index in range(305):
        jobs_module.JobManager._append_event(job, "info", f"log-{index}")

    assert len(job["logs"]) == 300
    assert job["logs"][0]["seq"] == 6
    assert job["logs"][-1]["seq"] == 305
    assert job["next_event_seq"] == 306


def test_pipeline_log_handler_captures_batch_child_threads(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    handler = manager._pipeline_log_handler(job["id"], job["generation"])
    logger = logging.getLogger("main")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        worker = threading.Thread(target=lambda: logger.error("批次 6 漏译 #230"))
        worker.start()
        worker.join()
    finally:
        logger.removeHandler(handler)

    assert manager.get(job["id"])["logs"][-1]["message"] == "批次 6 漏译 #230"


def test_pipeline_log_handler_redacts_sensitive_record_before_persisting(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    handler = manager._pipeline_log_handler(job["id"], job["generation"])
    logger = logging.getLogger("long_video")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    malicious = (
        "Authorization: Bearer sk-live-handler Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )
    try:
        logger.error(malicious)
    finally:
        logger.removeHandler(handler)

    persisted = manager._path(job["id"]).read_text(encoding="utf-8")
    for secret in (
        "sk-live-handler",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
    ):
        assert secret not in persisted
    assert len(manager.get(job["id"])["logs"][-1]["message"]) <= 240


def test_pipeline_failure_message_uses_only_structured_manifest_fields(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    output = Path(job["workspace"]) / "final.zh.srt"
    manifest = output.with_name("final.round1.zh.srt.manifest.json")
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "batches": {
            "5": {
                "state": "failed",
                "error": "PRIVATE_SUBTITLE_LINE sk-live-manifest",
                "error_code": "invalid_provider_response",
                "upstream_status": 400,
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    message = manager._pipeline_failure_message(output, 2)
    assert "Round 1 第 6 批失败" in message
    assert "HTTP 400" in message
    assert "PRIVATE_SUBTITLE_LINE" not in message
    assert "sk-live-manifest" not in message


def test_pipeline_failure_message_does_not_trust_legacy_error_text(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    output = Path(job["workspace"]) / "final.zh.srt"
    manifest = output.with_name("final.round1.zh.srt.manifest.json")
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "batches": {
            "0": {
                "state": "failed",
                "error": "PRIVATE_SUBTITLE_LINE Bearer sk-live-legacy",
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    message = manager._pipeline_failure_message(output, 2)
    assert "Round 1 第 1 批失败" in message
    assert "PRIVATE_SUBTITLE_LINE" not in message
    assert "sk-live-legacy" not in message


def test_review_update_generates_separate_srt(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    output_dir = Path(job["workspace"])
    final = output_dir / "final.zh.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n自动译文\n",
        encoding="utf-8-sig",
    )
    risk = output_dir / "final.zh.risk-queue.json"
    risk.write_text(json.dumps({"schema_version": 1, "items": [{
        "key": "risk-1", "subtitle_id": 1, "translated": "一轮译文",
        "round2_text": "自动译文", "state": "resolved",
    }]}, ensure_ascii=False), encoding="utf-8")
    job["artifacts"] = {final.name: str(final), risk.name: str(risk)}
    manager._save(job)

    manager.update_risk(
        job["id"], "risk-1", {"state": "resolved", "round2_text": "人工译文"}
    )

    reviewed = output_dir / "final.reviewed.zh.srt"
    assert "人工译文" in reviewed.read_text(encoding="utf-8-sig")
    assert "自动译文" in final.read_text(encoding="utf-8-sig")
    assert manager.get(job["id"])["artifacts"][reviewed.name] == str(reviewed)


def test_split_review_update_only_changes_human_state(manager, tmp_path):
    from pipeline.risk_queue_store import (
        ensure_review_state,
        load_review_state,
        save_generated_risks,
        save_round2_results,
    )

    job = _write_job(manager, tmp_path, status="completed")
    output_dir = Path(job["workspace"])
    final = output_dir / "final.zh.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n自动译文\n",
        encoding="utf-8-sig",
    )
    generated = output_dir / "final.zh.risk.generated.json"
    results = output_dir / "final.zh.round2.results.json"
    review = output_dir / "final.zh.review-state.json"
    save_generated_risks(str(generated), [{
        "key": "risk-1", "subtitle_id": 1,
        "start": "00:00:00,000", "end": "00:00:01,000",
        "translated": "一轮译文", "reasons": ["text_conflict"],
    }])
    save_round2_results(str(results), [{
        "key": "risk-1", "state": "resolved", "round2_text": "自动译文",
    }])
    ensure_review_state(str(review))
    generated_before = generated.read_bytes()
    results_before = results.read_bytes()
    job["artifacts"] = {
        final.name: str(final), generated.name: str(generated),
        results.name: str(results), review.name: str(review),
    }
    manager._save(job)

    manager.update_risk(
        job["id"], "risk-1",
        {"state": "resolved", "round2_text": "人工译文"},
    )

    assert generated.read_bytes() == generated_before
    assert results.read_bytes() == results_before
    assert load_review_state(str(review))[0]["text"] == "人工译文"
    reviewed = output_dir / "final.reviewed.zh.srt"
    assert "人工译文" in reviewed.read_text(encoding="utf-8-sig")


def test_manual_review_is_archived_but_only_explicit_choice_is_reused(
        manager, tmp_path):
    from pipeline.risk_queue_store import ensure_review_state, save_generated_risks
    from pipeline.translation_memory import TranslationMemoryStore

    job = _write_job(manager, tmp_path, status="completed")
    output_dir = Path(job["workspace"])
    final = output_dir / "final.zh.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n菲贝来了\n",
        encoding="utf-8-sig",
    )
    generated = output_dir / "final.zh.risk.generated.json"
    review = output_dir / "final.zh.review-state.json"
    save_generated_risks(str(generated), [{
        "key": "risk-name", "subtitle_id": 1,
        "start": "00:00:00,000", "end": "00:00:01,000",
        "english": "Phoebe is here", "capcut_en": "Phoebe is here",
        "translated": "菲贝来了", "reasons": ["term_mismatch"],
    }])
    ensure_review_state(str(review))
    job["artifacts"] = {
        final.name: str(final), generated.name: str(generated),
        review.name: str(review),
    }
    manager._save(job)

    manager.update_risk(job["id"], "risk-name", {
        "state": "resolved", "round2_text": "菲比来了",
        "memory_error_type": "人名",
    })
    stored = TranslationMemoryStore(
        jobs_module.TRANSLATION_MEMORY_PATH
    ).list_all()
    assert stored[0]["final"] == "菲比来了"
    assert stored[0]["approved"] is False

    manager.update_risk(job["id"], "risk-name", {
        "state": "resolved", "round2_text": "菲比来了",
        "memory_error_type": "人名", "remember_for_future": True,
    })
    approved = TranslationMemoryStore(
        jobs_module.TRANSLATION_MEMORY_PATH
    ).approved_for_sources(["Phoebe is here"])
    assert approved[0]["approved"] is True


def test_bulk_review_writes_human_state_and_reviewed_srt_once(
        manager, tmp_path, monkeypatch):
    from pipeline.risk_queue_store import (
        ensure_review_state,
        load_review_state,
        save_generated_risks,
        save_round2_results,
    )

    job = _write_job(manager, tmp_path, status="completed")
    output_dir = Path(job["workspace"])
    final = output_dir / "final.zh.srt"
    final.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧一\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n旧二\n",
        encoding="utf-8-sig",
    )
    generated = output_dir / "final.zh.risk.generated.json"
    results = output_dir / "final.zh.round2.results.json"
    review = output_dir / "final.zh.review-state.json"
    save_generated_risks(str(generated), [
        {
            "key": "risk-1", "subtitle_id": 1,
            "start": "00:00:00,000", "end": "00:00:01,000",
            "translated": "旧一", "reasons": ["term_mismatch"],
        },
        {
            "key": "risk-2", "subtitle_id": 2,
            "start": "00:00:01,000", "end": "00:00:02,000",
            "translated": "旧二", "reasons": ["term_mismatch"],
        },
    ])
    save_round2_results(str(results), [])
    ensure_review_state(str(review))
    job["artifacts"] = {
        final.name: str(final), generated.name: str(generated),
        results.name: str(results), review.name: str(review),
    }
    manager._save(job)
    writes = 0
    original = manager._write_reviewed_srt

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_write_reviewed_srt", count_write)
    manager.bulk_update_risks(job["id"], [
        {"key": "risk-1", "state": "resolved", "text": "新一"},
        {"key": "risk-2", "state": "verified", "text": "旧二"},
    ])

    assert writes == 1
    state = {item["key"]: item for item in load_review_state(str(review))}
    assert state["risk-1"]["text"] == "新一"
    reviewed = (output_dir / "final.reviewed.zh.srt").read_text(
        encoding="utf-8-sig"
    )
    assert "新一" in reviewed
    assert "旧二" in reviewed


def test_weak_risk_round_two_is_generated_on_demand(
        manager, tmp_path, monkeypatch):
    from pipeline import long_video as long_video_module
    from pipeline.risk_queue_store import (
        ensure_review_state,
        load_round2_results,
        save_generated_risks,
        save_round2_results,
    )
    from pipeline.translate import llm as llm_module

    job = _write_job(manager, tmp_path, status="completed", dry_run=False)
    job["options"]["proxy"] = "http://127.0.0.1:7890"
    job["options"]["round2_model"] = "review-model"
    output_dir = Path(job["workspace"])
    generated = output_dir / "final.zh.risk.generated.json"
    results = output_dir / "final.zh.round2.results.json"
    review = output_dir / "final.zh.review-state.json"
    save_generated_risks(str(generated), [{
        "key": "risk-1",
        "subtitle_id": 1,
        "start": "00:00:00,000",
        "end": "00:00:02,000",
        "english": "A long subtitle",
        "capcut_en": "A long subtitle",
        "whisper_en": "",
        "translated": "一条较长的字幕",
        "reasons": ["reading_speed"],
    }])
    save_round2_results(str(results), [])
    ensure_review_state(str(review))
    job["artifacts"] = {
        generated.name: str(generated),
        results.name: str(results),
        review.name: str(review),
    }
    manager._save(job)

    class Translator:
        def _call_api(self, prompt):
            assert json.loads(prompt)["items"][0]["id"] == 1
            return json.dumps({
                "results": [{
                    "id": 1,
                    "decision": "keep",
                    "text": "一条较长的字幕",
                    "confidence": 0.96,
                    "reason": "原译准确",
                }],
            }, ensure_ascii=False), 20, 5

    monkeypatch.setattr(
        long_video_module, "_load_glossary",
        lambda game, data_dir=None: {},
    )
    translator_options = {}

    def create_translator(**kwargs):
        translator_options.update(kwargs)
        return Translator()

    monkeypatch.setattr(llm_module, "create_translator", create_translator)

    merged = manager.generate_round2_suggestion(
        job["id"], "risk-1", "secret"
    )

    assert merged[0]["round2_text"] == "一条较长的字幕"
    assert translator_options["proxy"] == "http://127.0.0.1:7890"
    assert translator_options["model"] == "review-model"
    payload = load_round2_results(str(results))
    assert payload["items"][0]["on_demand"] is True
    assert payload["metrics"]["on_demand_tokens_in"] == 20
    assert payload["metrics"]["on_demand_tokens_out"] == 5


def test_completed_job_preserves_download_artifacts(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued")
    video = Path(job["workspace"]) / "input.mp4"
    video.write_bytes(b"video")
    job["artifacts"] = {"video": str(video)}
    manager._save(job)

    def fake_pipeline(**kwargs):
        output = Path(kwargs["output"])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n译文\n",
            encoding="utf-8-sig",
        )
        (output.parent / "final.zh.pipeline.metrics.json").write_text(
            json.dumps({
                "schema_version": 1,
                "round1": {"tokens_in": 120, "tokens_out": 30},
                "round2": {"tokens_in": 20, "tokens_out": 5},
                "quality": {"risk_rate": 0.1},
            }),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(long_video, "run_long_video", fake_pipeline)
    manager._run(job["id"], "", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "completed"
    # video 是下载中间产物，完成后归档进 过程文件/（artifacts 引用已更新）
    assert Path(current["artifacts"]["video"]).name == "input.mp4"
    assert Path(current["artifacts"]["video"]).parent.name == "过程文件"
    assert not video.exists()
    assert "final.zh.srt" in current["artifacts"]
    process_dir = Path(job["workspace"]) / "过程文件"
    assert current["technical_files_folder"] == "过程文件"
    assert not (Path(job["workspace"]) / "final.zh.pipeline.metrics.json").exists()
    assert (process_dir / "final.zh.pipeline.metrics.json").is_file()
    assert Path(current["artifacts"]["final.zh.pipeline.metrics.json"]) == (
        process_dir / "final.zh.pipeline.metrics.json"
    )
    assert current["metrics"]["round1"]["tokens_in"] == 120
    assert current["metrics"]["quality"]["risk_rate"] == 0.1


def test_translation_uses_the_job_proxy_for_model_requests(
        manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued")
    job["options"]["proxy"] = "http://127.0.0.1:7890"
    job["options"]["model"] = "round-one-model"
    job["options"]["round2_model"] = "round-two-model"
    job["metadata"] = {
        "title": "Phoebe Story Reaction",
        "channel": "Example Creator",
    }
    manager._save(job)
    received = {}

    def fake_pipeline(**kwargs):
        received.update(kwargs)
        Path(kwargs["output"]).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n译文\n",
            encoding="utf-8-sig",
        )
        return 0

    monkeypatch.setattr(long_video, "run_long_video", fake_pipeline)
    manager._run(job["id"], "", job["generation"])

    assert received["proxy"] == "http://127.0.0.1:7890"
    assert received["model"] == "round-one-model"
    assert received["round2_model"] == "round-two-model"
    assert received["video_context"] == {
        "title": "Phoebe Story Reaction",
        "channel": "Example Creator",
    }
    assert manager.get(job["id"])["status"] == "completed"


def test_config_snapshot_is_reused_without_secrets(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="queued")
    manager.secrets[job["id"]] = "secret-value"

    first = manager._ensure_config_snapshot(job["id"], job["generation"])
    second = manager._ensure_config_snapshot(job["id"], job["generation"])

    current = manager.get(job["id"])
    assert first == second
    assert set(current["config_snapshot"]["sha256"]) == {
        "alias.json", "asr_corrections.json", "asr_corrections_en.json",
        "glossary.db", "music.txt",
        "prompt_template.txt", "tricky_terms.json", "ja_person_terms.json",
        "ja_place_terms.json", "ko_person_terms.json",
        "translation_memory.json",
        "prompts/ja-zh-CN.txt", "prompts/ko-zh-CN.txt",
        "prompts/en-zh-CN.txt",
        "prompts/source-reconstruct-ko.txt",
        "prompts/source-reconstruct-ja.txt",
    }
    assert "secret-value" not in manager._path(job["id"]).read_text(encoding="utf-8")


def test_config_snapshot_preserves_v2_japanese_translation_memory(
        manager, tmp_path, monkeypatch):
    source_root = tmp_path / "source-root"
    data = source_root / "data"
    data.mkdir(parents=True)
    for name, content in {
        "alias.json": "{}",
        "asr_corrections.json": '{"ko": "snapshot"}',
        "asr_corrections_en.json": '{"en": "snapshot"}',
        "music.txt": "[Music]\n",
        "prompt_template.txt": "{subtitles}\n{glossary}\n{format_example}\n{established_terms_section}\n{tricky_terms}",
        "tricky_terms.json": "[]",
        "ja_person_terms.json": "{}",
        "ja_place_terms.json": "{}",
        "ko_person_terms.json": "{}",
    }.items():
        (data / name).write_text(content, encoding="utf-8")
    sqlite3.connect(data / "glossary.db").close()
    (data / "prompts").mkdir()
    (data / "prompts" / "ja-zh-CN.txt").write_text("Japanese prompt", encoding="utf-8")
    (data / "translation_memory.json").write_text(json.dumps({
        "schema_version": 2,
        "kind": "translation-memory",
        "entries": [{
            "id": "ja-memory", "approved": True,
            "source_language": "ja", "target_language": "zh-CN",
            "source": "カルテジア", "source_normalized": "カルテジア",
            "final": "卡提希娅", "game": "wuwa",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "ROOT", source_root)
    job = _write_job(manager, tmp_path, status="queued")

    snapshot = Path(manager._ensure_config_snapshot(job["id"], job["generation"]))
    memory = json.loads((snapshot / "translation_memory.json").read_text(
        encoding="utf-8"
    ))

    assert memory["schema_version"] == 2
    assert memory["entries"] == [{
        "id": "ja-memory", "approved": True,
        "source_language": "ja", "target_language": "zh-CN",
        "source": "カルテジア", "source_normalized": "カルテジア",
        "final": "卡提希娅", "game": "wuwa",
    }]
    assert "translation_memory.json" in manager.get(job["id"])["config_snapshot"]["sha256"]
    assert (snapshot / "asr_corrections.json").read_text(
        encoding="utf-8"
    ) == '{"ko": "snapshot"}'
    assert (snapshot / "asr_corrections_en.json").read_text(
        encoding="utf-8"
    ) == '{"en": "snapshot"}'


def test_stale_worker_cannot_create_config_snapshot(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="queued")

    with pytest.raises(jobs_module.PipelineCancelled):
        manager._ensure_config_snapshot(job["id"], job["generation"] + 1)

    assert "config_snapshot" not in manager.get(job["id"])


def test_retry_requires_new_key_before_mutating_failed_job(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="failed", dry_run=False)

    with pytest.raises(ValueError, match="API key"):
        manager.retry_failed(job["id"], "")

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["generation"] == job["generation"]


def test_retry_requires_restart_for_resume_sensitive_setting_changes(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="failed", dry_run=True)
    manifest = Path(job["workspace"]) / "final.zh.srt.manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="从头重跑"):
        manager.retry_failed(
            job["id"], "", translation_options={"batch_size": 2}
        )

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["generation"] == job["generation"]
    assert manifest.exists()


def test_restart_applies_shared_translation_settings(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="failed", dry_run=True)
    manifest = Path(job["workspace"]) / "final.zh.srt.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    restarted = manager.retry_failed(
        job["id"], "", restart=True,
        translation_options={
            "model": "new-model", "base_url": "", "batch_size": 2,
            "game": "wuwa", "local_whisper": False, "dry_run": True,
        },
    )

    assert restarted["status"] == "queued"
    assert restarted["options"]["model"] == "new-model"
    assert restarted["options"]["batch_size"] == 2
    assert not manifest.exists()


def test_completed_dry_run_can_restart_as_formal_translation(
        manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="completed", dry_run=True)
    workspace = Path(job["workspace"])
    job["url"] = "https://www.youtube.com/watch?v=abcdefghijk"
    manager._save(job)
    for name in ("final.round1.zh.srt", "final.round2.zh.srt", "final.zh.srt"):
        (workspace / name).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n[待翻译] Hello\n",
            encoding="utf-8",
        )
    submissions = []
    monkeypatch.setattr(
        manager, "_submit", lambda *args: submissions.append(args)
    )

    restarted = manager.retry_failed(
        job["id"], "real-key", restart=True,
        translation_options={
            "model": "new-model", "round2_model": "review-model",
            "base_url": "https://example.test/v1", "batch_size": 2,
            "game": "wuwa", "local_whisper": False, "dry_run": False,
        },
    )

    assert restarted["status"] == "queued"
    assert restarted["options"]["dry_run"] is False
    assert restarted["options"]["api_key_configured"] is True
    assert submissions
    assert submissions[0][1] == manager._run
    assert (workspace / "final.round1.dry-run.zh.srt").exists()
    assert (workspace / "final.round2.dry-run.zh.srt").exists()
    assert (workspace / "final.dry-run.zh.srt").exists()


def test_completed_real_translation_cannot_be_restarted_as_retry(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed", dry_run=False)

    with pytest.raises(ValueError, match="只能重试"):
        manager.retry_failed(
            job["id"], "real-key", restart=True,
            translation_options={"dry_run": False},
        )


def test_artifact_path_rejects_workspace_escape(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    outside = tmp_path / "outside.srt"
    outside.write_text("secret", encoding="utf-8")
    job["artifacts"] = {"outside.srt": str(outside)}
    with pytest.raises(ValueError, match="超出工作目录"):
        manager._save(job)
    jobs_module.atomic_json(manager._path(job["id"]), job)

    with pytest.raises(ValueError, match="超出工作目录"):
        manager.artifact_path(job["id"], "outside.srt")


def test_job_json_persists_task_relative_paths_but_runtime_hydrates_them(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    workspace = Path(job["workspace"])
    config = workspace / "config" / "generation-1"
    artifact = workspace / "过程文件" / "debug.json"
    job["config_snapshot"] = {"path": str(config), "sha256": {}}
    job["artifacts"] = {"debug": str(artifact)}
    manager._save(job)

    raw = json.loads(manager._path(job["id"]).read_text(encoding="utf-8"))
    encoded = json.dumps(raw, ensure_ascii=False)
    assert raw["workspace"] == "."
    assert raw["inputs"]["capcut_en"] == "capcut.en.srt"
    assert raw["artifacts"]["debug"] == "过程文件/debug.json"
    assert raw["config_snapshot"]["path"] == "config/generation-1"
    assert str(tmp_path) not in encoded

    loaded = manager.get(job["id"])
    assert loaded["workspace"] == str(workspace)
    assert loaded["inputs"]["capcut_en"] == str(workspace / "capcut.en.srt")
    assert loaded["artifacts"]["debug"] == str(artifact)
    assert loaded["config_snapshot"]["path"] == str(config)


def test_new_jobs_respect_count_and_disk_quotas(manager, tmp_path, monkeypatch):
    existing = _write_job(manager, tmp_path, status="completed")
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 1)

    with pytest.raises(ValueError, match="任务数量已达到本机上限"):
        manager.create_from_url(
            "https://www.youtube.com/watch?v=example",
            {"youtube_auth": "none"},
        )

    monkeypatch.setattr(jobs_module, "MAX_JOBS", 100)
    monkeypatch.setattr(
        jobs_module, "MAX_JOB_DISK_BYTES", manager._disk_usage() - 1,
    )
    with pytest.raises(ValueError, match="任务文件占用空间已达到本机上限"):
        manager.create_from_url(
            "https://www.youtube.com/watch?v=example",
            {"youtube_auth": "none"},
        )
    assert manager.get(existing["id"])["status"] == "completed"


def test_job_summary_rename_archive_and_safe_cleanup(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    workspace = Path(job["workspace"])
    video = workspace / "source.mp4"
    audio = workspace / "source.wav"
    final = workspace / "final.zh.srt"
    manifest = workspace / "final.zh.srt.manifest.json"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    final.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    job["artifacts"] = {
        "video": str(video),
        "audio": str(audio),
        "final.zh.srt": str(final),
        "final.zh.srt.manifest.json": str(manifest),
    }
    manager._save(job)

    renamed = manager.update_metadata(job["id"], custom_name="演唱会", archived=True)
    assert renamed["custom_name"] == "演唱会"
    assert renamed["archived"] is True
    assert renamed["disk_bytes"] > 0

    cleaned = manager.cleanup(job["id"], "video")
    assert cleaned["removed"] == ["video", "audio"]
    assert not video.exists()
    assert not audio.exists()
    assert final.exists()
    assert "源媒体" in manager.get(job["id"])["logs"][-1]["message"]

    cleaned = manager.cleanup(job["id"], "technical")
    assert "final.zh.srt.manifest.json" in cleaned["removed"]
    assert not manifest.exists()
    assert final.exists()


def test_organize_delivery_files_groups_technical_artifacts_without_deleting(
        manager, tmp_path):
    job = _write_job(manager, tmp_path, status="completed")
    workspace = Path(job["workspace"])
    files = {
        "video": workspace / "source.mp4",
        "thumbnail": workspace / "cover.jpg",
        "final.zh.srt": workspace / "final.zh.srt",
        "final.reviewed.zh.srt": workspace / "final.reviewed.zh.srt",
        "youtube.en.srt": workspace / "youtube.en.srt",
        "reference.whisper.en.srt": workspace / "reference.whisper.en.srt",
        "final.round2.zh.srt": workspace / "final.round2.zh.srt",
        "final.zh.risk.generated.json": workspace / "final.zh.risk.generated.json",
    }
    for name, path in files.items():
        if path.suffix == ".mp4":
            path.write_bytes(b"video")
        else:
            path.write_text(name, encoding="utf-8")
    config = workspace / "config"
    evidence = workspace / "final.zh.evidence"
    config.mkdir()
    evidence.mkdir()
    (config / "prompt.txt").write_text("technical", encoding="utf-8")
    (evidence / "name-proof.json").write_text("technical", encoding="utf-8")
    job["artifacts"] = {name: str(path) for name, path in files.items()}
    manager._save(job)

    organized = manager.organize(job["id"])

    assert set(organized["moved"]) == {
        "capcut.en.srt",
        "whisper.en.srt",
        "youtube.en.srt",
        "reference.whisper.en.srt",
        "final.round2.zh.srt",
        "final.zh.risk.generated.json",
        "source.mp4",
        "cover.jpg",
    }
    assert organized["moved_directories"] == ["config", "final.zh.evidence"]
    technical = workspace / "过程文件"
    assert (technical / "config" / "prompt.txt").is_file()
    assert (technical / "final.zh.evidence" / "name-proof.json").is_file()
    assert (workspace / "job.json").is_file()
    for name in ("final.zh.srt", "final.reviewed.zh.srt"):
        assert files[name].is_file()
    # video/thumbnail 是下载中间产物，归档进 过程文件/，顶层不保留
    assert (technical / "source.mp4").is_file()
    assert (technical / "cover.jpg").is_file()
    for name in {
        "capcut.en.srt",
        "whisper.en.srt",
        "youtube.en.srt",
        "reference.whisper.en.srt",
        "final.round2.zh.srt",
        "final.zh.risk.generated.json",
    }:
        assert not (workspace / name).exists()
        assert (technical / name).is_file()
    assert set(organized["job"]["artifacts"]) == {
        "video", "thumbnail", "final.zh.srt", "final.reviewed.zh.srt",
        "youtube.en.srt", "reference.whisper.en.srt", "final.round2.zh.srt",
        "final.zh.risk.generated.json",
    }
    assert Path(organized["job"]["artifacts"]["video"]).parent == technical
    assert Path(organized["job"]["artifacts"]["thumbnail"]).parent == technical
    assert Path(organized["job"]["inputs"]["capcut_en"]).parent == technical
    assert Path(organized["job"]["inputs"]["whisper_en"]).parent == technical


def test_organize_retries_transient_windows_handle_conflict(
        manager, tmp_path, monkeypatch):
    """真实 bug（2026-09-06 任务 6311f11a3cad）：真实翻译刚用完
    config/<generation>/glossary.db，Windows 上 SQLite 句柄的释放等 GC，
    完成后的文件整理 shutil.move 目录瞬间 WinError 32 → 整次任务被打成
    pipeline_error。须 gc.collect()+短重试消化瞬时占用，重试耗尽才上抛。"""
    job = _write_job(manager, tmp_path, status="completed")
    workspace = Path(job["workspace"])
    (workspace / "youtube.en.srt").write_text("payload", encoding="utf-8")

    real_move = jobs_module.shutil.move
    attempts = {"n": 0}

    def flaky_move(src, dst, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_move(src, dst, **kwargs)

    monkeypatch.setattr(jobs_module.shutil, "move", flaky_move)

    organized = manager.organize(job["id"])

    assert attempts["n"] >= 2, "应至少重试一次而不是直接上抛"
    assert "youtube.en.srt" in organized["moved"]
    assert (Path(organized["folder"]) / "youtube.en.srt").read_text(
        encoding="utf-8",
    ) == "payload"


def test_corrupted_config_snapshot_fails_fast_before_translation(
        manager, tmp_path):
    """真实事故（2026-09-06 任务 6311f11a3cad）：organize 半移使快照缺
    alias.json，之后每次 resume 都在流水线深处 FileNotFoundError(alias.json)，
    用户只看到含糊的"翻译任务失败"。启动前按建快照时记录的 sha256 做完整性
    校验，损坏必须变成可行动的明确错误（glossary.db 打开会幂等重写字节，
    只验证存在）。"""
    job = _write_job(manager, tmp_path, status="ready_for_translation")
    workspace = Path(job["workspace"])
    snap = workspace / "config" / "generation-3-abc123"
    snap.mkdir(parents=True)
    (snap / "glossary.db").write_bytes(b"db")
    job["config_snapshot"] = {
        "path": str(snap),
        "sha256": {"glossary.db": "recorded", "alias.json": "recorded"},
    }
    manager._save(job)

    with pytest.raises(FileNotFoundError, match="损坏"):
        manager._ensure_config_snapshot(job["id"], job["generation"])


def test_snapshot_integrity_passes_when_files_match(manager, tmp_path):
    """回归护栏：完好快照（glossary.db 字节可变）原样放行，不误重建。"""
    job = _write_job(manager, tmp_path, status="ready_for_translation")
    workspace = Path(job["workspace"])
    snap = workspace / "config" / "generation-3-abc123"
    snap.mkdir(parents=True)
    (snap / "glossary.db").write_bytes(b"db-bytes")
    body = b"{}"
    (snap / "alias.json").write_bytes(body)
    job["config_snapshot"] = {
        "path": str(snap),
        "sha256": {
            "glossary.db": "mutated-by-open",
            "alias.json": hashlib.sha256(body).hexdigest(),
        },
    }
    manager._save(job)

    result = manager._ensure_config_snapshot(job["id"], job["generation"])

    assert Path(result).resolve() == snap.resolve()


def test_authentication_failure_sets_safe_error_code(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "bad-secret"

    def fail_pipeline(**kwargs):
        raise LLMAuthenticationError("API 401: invalid key", status_code=401)

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    manager._run(job["id"], "bad-secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "authentication_error"
    assert "API Key" in current["error"]
    assert "HTTP 401" in current["error"]
    assert "invalid key" not in current["error"]
    assert current["upstream_status"] == 401
    assert current["options"]["api_key_configured"] is False
    assert job["id"] not in manager.secrets
    assert "Traceback" not in json.dumps(current, ensure_ascii=False)


def test_pipeline_failure_redacts_untrusted_error_from_job_events_file_and_logs(
        manager, tmp_path, monkeypatch, caplog):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "runtime-secret"
    malicious = (
        "Authorization: Bearer sk-live-web-job Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )

    def fail_pipeline(**kwargs):
        raise RuntimeError(malicious)

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    caplog.set_level(logging.INFO)
    manager._run(job["id"], "runtime-secret", job["generation"])

    current = manager.get(job["id"])
    persisted = manager._path(job["id"]).read_text(encoding="utf-8")
    exposed = json.dumps(current, ensure_ascii=False) + persisted + caplog.text
    for secret in (
        "sk-live-web-job",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
    ):
        assert secret not in exposed
    assert current["error_code"] == "pipeline_error"
    assert len(current["error"]) <= 240


def test_download_failure_redacts_untrusted_error_from_job_events_file_and_logs(
        manager, tmp_path, monkeypatch, caplog):
    job = _write_job(manager, tmp_path, status="queued")
    current = manager._load(job["id"])
    current["url"] = "https://www.youtube.com/watch?v=example"
    current["options"].update({
        "reference_strategy": "youtube",
        "youtube_auth": "none",
        "download_video": True,
        "download_subtitles": True,
        "download_thumbnail": False,
    })
    manager._save(current)
    malicious = (
        "api_key=sk-live-download Authorization: Bearer bearer-download "
        "Cookie: session=private password=download-password "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )

    def fail_download(*args, **kwargs):
        raise RuntimeError(malicious)

    monkeypatch.setattr(downloads_module, "run_download", fail_download)
    caplog.set_level(logging.INFO)
    manager._run_download(job["id"], "", job["generation"])

    loaded = manager.get(job["id"])
    persisted = manager._path(job["id"]).read_text(encoding="utf-8")
    exposed = json.dumps(loaded, ensure_ascii=False) + persisted + caplog.text
    for secret in (
        "sk-live-download",
        "bearer-download",
        "session=private",
        "download-password",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
    ):
        assert secret not in exposed
    assert loaded["error_code"] == "download_error"
    assert len(loaded["error"]) <= 240


def test_close_waits_for_active_worker_before_closing_credentials():
    order = []
    worker_started = threading.Event()
    release_worker = threading.Event()
    store_closed = threading.Event()

    class OrderedStore:
        def close(self):
            order.append("store-close")
            store_closed.set()

    manager = jobs_module.JobManager(OrderedStore())

    def active_worker():
        order.append("worker-start")
        worker_started.set()
        assert release_worker.wait(5)
        order.append("worker-end")

    manager._submit("active", active_worker)
    assert worker_started.wait(2)
    closer = threading.Thread(target=manager.close)
    closer.start()
    try:
        deadline = time.monotonic() + 2
        while getattr(manager, "_accepting_work", True) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager._accepting_work is False
        assert not store_closed.is_set()
        with pytest.raises(RuntimeError, match="关闭"):
            manager._submit("late", lambda: None)
    finally:
        release_worker.set()
        closer.join(5)
        manager.close()

    assert not closer.is_alive()
    assert order.index("worker-end") < order.index("store-close")


def test_close_treats_credential_cleanup_failure_as_best_effort(caplog):
    class FailingStore:
        def close(self):
            raise OSError(
                "password=private C:\\FixtureHome\\Alice\\private-cookie.cookie"
            )

    manager = jobs_module.JobManager(FailingStore())
    caplog.set_level(logging.WARNING)

    manager.close()

    assert "password=private" not in caplog.text
    assert "C:\\FixtureHome\\Alice" not in caplog.text
    with pytest.raises(RuntimeError, match="关闭"):
        manager._submit("late", lambda: None)


def test_closed_manager_rejects_url_job_before_queueing_cookie(
        manager, tmp_path, monkeypatch):
    workspace = tmp_path / "jobs" / "deadbeefcafe"
    monkeypatch.setattr(
        jobs_module.uuid,
        "uuid4",
        lambda: type("FixedUUID", (), {"hex": "deadbeefcafe"})(),
    )
    queued = []
    monkeypatch.setattr(
        manager.private_cookie_store,
        "queue",
        lambda *args: queued.append(args),
    )
    manager.close()

    with pytest.raises(RuntimeError, match="关闭"):
        manager.create_from_url(
            "https://www.youtube.com/watch?v=example",
            {"youtube_auth": "file"},
            youtube_cookie=b"PRIVATE_COOKIE",
        )

    assert queued == []
    assert not workspace.exists()


def test_closed_manager_rejects_translation_before_mutating_job(
        manager, tmp_path):
    job = _write_job(
        manager, tmp_path, status="ready_for_translation", dry_run=False,
    )
    before = manager.get(job["id"])
    manager.close()

    with pytest.raises(RuntimeError, match="关闭"):
        manager.start_translation(job["id"], "PRIVATE_API_KEY")

    current = manager.get(job["id"])
    assert current["status"] == before["status"]
    assert current["generation"] == before["generation"]
    assert job["id"] not in manager.secrets


def test_delete_waits_for_cancelled_worker_to_stop(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="cancelled")

    class RunningFuture:
        def done(self):
            return False

    manager.futures[job["id"]] = RunningFuture()

    with pytest.raises(ValueError, match="仍在停止"):
        manager.delete(job["id"])

    assert Path(job["workspace"]).exists()


def test_manager_marks_running_jobs_interrupted_on_startup(tmp_path, monkeypatch):
    root = tmp_path / "recovery-jobs"
    workspace = root / "stale"
    workspace.mkdir(parents=True)
    job = {
        "id": "stale", "created_at": jobs_module.now(), "updated_at": jobs_module.now(),
        "status": "running", "stage": "Translating", "generation": 2,
        "logs": [], "workspace": str(workspace), "artifacts": {},
        "inputs": {}, "options": {}, "error": None,
    }
    jobs_module.atomic_json(workspace / "job.json", job)
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)

    recovered = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "recovery-private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    try:
        recovered.recover_interrupted_jobs()
        current = recovered.get("stale")
        assert current["status"] == "failed"
        assert current["generation"] == 3
        assert current["logs"][-1]["seq"] == 1
    finally:
        recovered.close()


def test_whisper_cleanup_failure_with_valid_srt_is_not_fatal(manager, monkeypatch):
    """Regression: Windows temp-wav cleanup PermissionError makes whisper exit
    non-zero, but the SRT is already written and usable. The download step must
    NOT fail the job in that case (was: '本地 Whisper 未生成可用英语参考')."""
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    def fake_download(url, output_dir, **kwargs):
        output = Path(output_dir)
        video = output / "video.mp4"
        video.write_bytes(b"video")
        return {"video": str(video)}

    class Quality:
        def __init__(self, usable):
            self.usable = usable

        def to_dict(self):
            return {"usable": self.usable, "score": 100 if self.usable else 0}

    monkeypatch.setattr(downloads_module, "run_download", fake_download)
    monkeypatch.setattr(
        source_quality, "assess_english_srt",
        lambda path: Quality(bool(path and Path(path).exists())),
    )

    def fake_whisper(command, cancel_check=None, emit=None):
        # SRT written successfully, but process exits non-zero (cleanup fail)
        output = Path(command[command.index("-o") + 1])
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello there\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(long_video, "_run_cancellable", fake_whisper)
    job = manager.create_from_url(
        "https://youtu.be/abcdefghi",
        {
            "workflow_mode": "quick",
            "reference_strategy": "whisper",
            "download_video": True,
            "download_subtitles": False,
            "download_thumbnail": False,
            "source_language": "en",
        },
    )

    manager._run_download(job["id"], "", 0)

    current = manager.get(job["id"])
    assert current["status"] == "ready_for_translation", current.get("error")
    assert current["reference_source"] == "whisper"


# ============================================================
# P0-2：有界自动续跑（worker 侧）
# ============================================================


def test_auto_resume_gives_up_after_max_attempts(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(jobs_module.Config, "LLM_AUTO_RESUME_MAX", 3)
    monkeypatch.setattr(
        jobs_module.Config, "LLM_AUTO_RESUME_BASE_DELAY_SECONDS", 30
    )
    sleeps = []
    monkeypatch.setattr(jobs_module.time, "sleep", lambda s: sleeps.append(s))
    calls = []

    def fail_pipeline(**kwargs):
        calls.append(1)
        raise LLMTransientError("API 503: 上游网关错误", status_code=503)

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    manager._run(job["id"], "secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "provider_unavailable"
    assert len(calls) == 4  # 1 次初始 + 3 次自动续跑
    assert current["auto_resume"]["attempts"] == 4
    delays = [
        entry["next_delay_seconds"]
        for entry in current["auto_resume"]["history"]
        if "next_delay_seconds" in entry
    ]
    assert delays == [30, 60, 120]  # 指数退避序列
    assert sum(sleeps) == 210  # 30+60+120 个 1 秒 tick
    assert job["id"] not in manager.secrets  # 终态才清 secret


@pytest.mark.parametrize("exc,expected_code", [
    (LLMQuotaError("API 403: 账户额度不足", status_code=403), "quota_error"),
    (LLMAuthenticationError("API 401: invalid", status_code=401),
     "authentication_error"),
    (LLMConfigurationError("API 400: bad config", status_code=400),
     "configuration_error"),
])
def test_auto_resume_never_retries_non_retryable(
        manager, tmp_path, monkeypatch, exc, expected_code):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(jobs_module.time, "sleep", lambda s: None)
    calls = []

    def fail_pipeline(**kwargs):
        calls.append(1)
        raise exc

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    manager._run(job["id"], "secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["error_code"] == expected_code
    assert len(calls) == 1  # 欠费/鉴权/配置：零自动续跑


def test_auto_resume_wait_is_interrupted_by_cancel(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(jobs_module.Config, "LLM_AUTO_RESUME_MAX", 3)
    monkeypatch.setattr(
        jobs_module.Config, "LLM_AUTO_RESUME_BASE_DELAY_SECONDS", 30
    )
    calls = []

    def fail_pipeline(**kwargs):
        calls.append(1)
        raise LLMTransientError("API 503: 上游网关错误", status_code=503)

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] == 2:
            manager.cancel(job["id"])  # bump generation → 等待应被打断

    monkeypatch.setattr(jobs_module.time, "sleep", fake_sleep)
    manager._run(job["id"], "secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "cancelled"  # 终态不被续跑覆盖
    assert len(calls) == 1  # 没有第二次 _run_attempt


def test_auto_resume_succeeds_on_second_attempt(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(jobs_module.Config, "LLM_AUTO_RESUME_MAX", 3)
    monkeypatch.setattr(
        jobs_module.Config, "LLM_AUTO_RESUME_BASE_DELAY_SECONDS", 1
    )
    monkeypatch.setattr(jobs_module.time, "sleep", lambda s: None)
    calls = []

    def flaky_pipeline(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise LLMTransientError("API 503: 上游网关错误", status_code=503)
        Path(kwargs["output"]).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n译文\n", encoding="utf-8-sig",
        )
        return 0

    monkeypatch.setattr(long_video, "run_long_video", flaky_pipeline)
    manager._run(job["id"], "secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "completed"
    assert len(calls) == 2
    assert job["id"] not in manager.secrets  # 成功后才清 secret


def test_start_translation_resets_auto_resume_summary(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="failed", dry_run=False)
    job["auto_resume"] = {
        "attempts": 4, "last_error_code": "provider_unavailable", "history": [],
    }
    manager._save(job)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(manager, "_submit", lambda *args: None)

    manager.start_translation(job["id"], "secret")

    assert manager.get(job["id"]).get("auto_resume") is None


def test_auto_resume_disabled_when_max_is_zero(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="queued", dry_run=False)
    manager.secrets[job["id"]] = "secret"
    monkeypatch.setattr(jobs_module.Config, "LLM_AUTO_RESUME_MAX", 0)
    monkeypatch.setattr(jobs_module.time, "sleep", lambda s: None)
    calls = []

    def fail_pipeline(**kwargs):
        calls.append(1)
        raise LLMTransientError("API 503: 上游网关错误", status_code=503)

    monkeypatch.setattr(long_video, "run_long_video", fail_pipeline)
    manager._run(job["id"], "secret", job["generation"])

    current = manager.get(job["id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "provider_unavailable"
    assert len(calls) == 1  # MAX=0 → 行为同现状，不续跑


class _FakeFuture:
    """Stand-in for a worker future tracked by ``JobManager.futures``."""

    def __init__(self, done: bool = False):
        self._done = done
        self.cancel_calls = 0

    def done(self) -> bool:
        return self._done

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return True


def _age_job(manager, job_id: str, minutes: int, *, heartbeat_minutes=None):
    """Backdate a job's activity stamps so the watchdog sees it as stale.

    ``heartbeat_minutes`` adds one log event.  Events are the real liveness
    signal: ``updated_at`` only moves on state transitions, so a healthy long
    download keeps an old ``updated_at`` while still emitting progress.
    """
    job = manager._load(job_id)
    reference = datetime.now().astimezone()
    job["updated_at"] = (
        reference - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")
    if heartbeat_minutes is not None:
        job["logs"] = [{
            "seq": 1,
            "at": (
                reference - timedelta(minutes=heartbeat_minutes)
            ).isoformat(timespec="seconds"),
            "kind": "info",
            "message": "下载进度 12.0% | 1.2MiB/s | ETA 03:00",
        }]
        job["next_event_seq"] = 2
    manager._save(job)


def test_watchdog_thresholds_have_safe_defaults():
    assert jobs_module.STUCK_JOB_TIMEOUT_MINUTES == 30
    assert jobs_module.STUCK_JOB_CHECK_INTERVAL_SECONDS == 300


def test_watchdog_resets_running_job_whose_worker_is_gone(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    manager.secrets[job["id"]] = "api-secret"
    _age_job(manager, job["id"], 45)

    actions = manager.reset_stuck_jobs()

    current = manager.get(job["id"])
    assert [(item["job_id"], item["action"]) for item in actions] == [
        (job["id"], "reset"),
    ]
    assert current["status"] == "failed"
    assert current["error_code"] == "interrupted"
    assert current["generation"] == job["generation"] + 1
    assert current["options"]["api_key_configured"] is False
    assert job["id"] not in manager.secrets
    assert any(
        event["kind"] == "error" and "可从失败批次继续" in event["message"]
        for event in current["logs"]
    )


def test_watchdog_treats_a_finished_future_as_an_orphan(manager, tmp_path):
    """线程已退出却仍写着 running 的任务，没人会再来收尾。"""
    job = _write_job(manager, tmp_path, status="running")
    manager.futures[job["id"]] = _FakeFuture(done=True)
    _age_job(manager, job["id"], 45)

    actions = manager.reset_stuck_jobs()

    assert [item["action"] for item in actions] == ["reset"]
    assert manager.get(job["id"])["status"] == "failed"


def test_watchdog_leaves_a_fresh_job_alone(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")

    assert manager.reset_stuck_jobs() == []
    assert manager.get(job["id"])["status"] == "running"


def test_watchdog_only_warns_while_the_worker_is_alive(manager, tmp_path):
    """红线：长视频下载/转码可能远超阈值，活线程绝不能在第一次扫描就被杀。"""
    job = _write_job(manager, tmp_path, status="running")
    future = _FakeFuture()
    manager.futures[job["id"]] = future
    _age_job(manager, job["id"], 40)

    actions = manager.reset_stuck_jobs()

    current = manager.get(job["id"])
    assert [item["action"] for item in actions] == ["warned"]
    assert current["status"] == "running"
    assert current["generation"] == job["generation"]
    assert future.cancel_calls == 1
    warnings = [
        event for event in current["logs"]
        if event["kind"] == "error" and "疑似卡死" in event["message"]
    ]
    assert len(warnings) == 1


def test_watchdog_does_not_repeat_the_same_warning(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    manager.futures[job["id"]] = _FakeFuture()
    _age_job(manager, job["id"], 40)

    assert [item["action"] for item in manager.reset_stuck_jobs()] == ["warned"]
    assert manager.reset_stuck_jobs() == []

    current = manager.get(job["id"])
    assert len([
        event for event in current["logs"]
        if event["kind"] == "error" and "疑似卡死" in event["message"]
    ]) == 1
    assert current["status"] == "running"


def test_watchdog_never_resets_a_live_worker_even_at_double_threshold(
    manager, tmp_path,
):
    job = _write_job(manager, tmp_path, status="running")
    future = _FakeFuture()
    manager.futures[job["id"]] = future
    _age_job(manager, job["id"], 40)
    assert [item["action"] for item in manager.reset_stuck_jobs()] == ["warned"]

    _age_job(manager, job["id"], 75)
    actions = manager.reset_stuck_jobs()

    current = manager.get(job["id"])
    assert actions == []
    assert current["status"] == "running"
    assert current.get("error_code") is None
    assert current["generation"] == job["generation"]
    # Future.cancel() 是唯一允许的动作；运行中的线程通常不会被它强杀。
    assert future.cancel_calls == 1


def test_watchdog_trusts_log_heartbeat_over_a_stale_updated_at(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    manager.futures[job["id"]] = _FakeFuture()
    _age_job(manager, job["id"], 90, heartbeat_minutes=1)

    assert manager.reset_stuck_jobs() == []

    current = manager.get(job["id"])
    assert current["status"] == "running"
    assert current["logs"] == [
        event for event in current["logs"] if event["kind"] != "error"
    ]


def test_watchdog_never_touches_a_queued_job_waiting_for_the_worker(
    manager, tmp_path,
):
    """单 worker 串行：排在长任务后面等半小时是正常现象，不是故障。"""
    job = _write_job(manager, tmp_path, status="queued")
    future = _FakeFuture()
    manager.futures[job["id"]] = future
    _age_job(manager, job["id"], 120)

    assert manager.reset_stuck_jobs() == []
    assert manager.get(job["id"])["status"] == "queued"
    assert future.cancel_calls == 0


def test_watchdog_resets_queued_orphan_left_behind(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="queued")
    _age_job(manager, job["id"], 120)

    assert [item["action"] for item in manager.reset_stuck_jobs()] == ["reset"]
    assert manager.get(job["id"])["status"] == "failed"


@pytest.mark.parametrize("status", [
    "paused", "ready_for_translation", "waiting_capcut", "completed",
    "failed", "cancelled",
])
def test_watchdog_ignores_statuses_that_are_meant_to_be_idle(
    manager, tmp_path, status,
):
    job = _write_job(manager, tmp_path, status=status)
    _age_job(manager, job["id"], 240)

    assert manager.reset_stuck_jobs() == []
    assert manager.get(job["id"])["status"] == status


def test_watchdog_threshold_is_configurable(manager, tmp_path, monkeypatch):
    job = _write_job(manager, tmp_path, status="running")
    _age_job(manager, job["id"], 45)
    monkeypatch.setattr(jobs_module, "STUCK_JOB_TIMEOUT_MINUTES", 120)

    assert manager.reset_stuck_jobs() == []
    assert manager.get(job["id"])["status"] == "running"

    monkeypatch.setattr(jobs_module, "STUCK_JOB_TIMEOUT_MINUTES", 10)
    assert [item["action"] for item in manager.reset_stuck_jobs()] == ["reset"]


def test_stuck_job_report_exposes_progress_without_paths(manager, tmp_path):
    job = _write_job(manager, tmp_path, status="running")
    manager.futures[job["id"]] = _FakeFuture()
    _age_job(manager, job["id"], 40)
    manager.reset_stuck_jobs()

    report = manager.stuck_job_report()

    assert len(report) == 1
    entry = report[0]
    assert entry["job_id"] == job["id"]
    assert entry["status"] == "running"
    assert entry["worker_alive"] is True
    assert entry["warned"] is True
    assert entry["silent_minutes"] >= 40
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)


def test_stuck_job_report_is_empty_when_nothing_is_stuck(manager, tmp_path):
    _write_job(manager, tmp_path, status="running")

    assert manager.stuck_job_report() == []
