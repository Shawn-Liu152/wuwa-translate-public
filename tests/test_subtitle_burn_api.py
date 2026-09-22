import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.subtitle_burner import BurnCapability
from web import app as app_module
from web import jobs as jobs_module
from web.private_cookie_store import PrivateCookieStore
from web.user_settings import UserSettingsStore


class _CredentialStore:
    def read(self):
        return ""

    def write(self, _secret):
        return None

    def delete(self):
        return None


@pytest.fixture
def burn_api(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)
    manager = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    submissions = []
    monkeypatch.setattr(
        manager,
        "_submit_burn",
        lambda job_id, *args: submissions.append((job_id, *args)),
    )
    monkeypatch.setattr(app_module, "manager", manager)
    monkeypatch.setattr(
        app_module,
        "user_settings",
        UserSettingsStore(
            tmp_path / "settings.json",
            credential_store=_CredentialStore(),
        ),
    )
    session_token = secrets.token_urlsafe(32)
    bootstrap_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    app_module.configure_local_access(
        session_token=session_token,
        bootstrap_token=bootstrap_token,
        csrf_token=csrf_token,
        launch_challenge=secrets.token_urlsafe(32),
    )
    with TestClient(app_module.app) as client:
        response = client.post(
            "/api/session/bootstrap",
            headers={
                "origin": "http://testserver",
                "X-Subtitle-Bootstrap": bootstrap_token,
            },
        )
        assert response.status_code == 200
        client.headers["X-Subtitle-CSRF"] = csrf_token
        yield client, manager, root, submissions


def _write_deliverable_job(manager, root, *, delivery_status="deliverable"):
    job_id = "burn-api-job"
    workspace = root / job_id
    workspace.mkdir()
    video = workspace / "source.mp4"
    subtitle = workspace / "final.zh.srt"
    video.write_bytes(b"video")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好\n",
        encoding="utf-8",
    )
    manager._save({
        "id": job_id,
        "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(),
        "status": "completed",
        "stage": "Deliverable",
        "delivery_status": delivery_status,
        "progress": {"completed": 1, "total": 1},
        "generation": 2,
        "workspace": str(workspace),
        "inputs": {},
        "options": {"dry_run": False},
        "artifacts": {"video": str(video), subtitle.name: str(subtitle)},
        "logs": [],
        "error": None,
        "error_code": None,
    })
    return job_id


def test_burn_api_queues_once_and_never_exposes_local_paths(burn_api):
    client, manager, root, submissions = burn_api
    job_id = _write_deliverable_job(manager, root)

    first = client.post(f"/api/jobs/{job_id}/exports/burned-video")
    second = client.post(f"/api/jobs/{job_id}/exports/burned-video")

    assert first.status_code == 202
    assert second.status_code == 202
    payload = first.json()
    assert payload["status"] == "completed"
    assert payload["exports"]["burned_video"]["status"] == "queued"
    assert payload["exports"]["burned_video"]["style"] == "black-outline-white-2-v1"
    assert str(root) not in first.text
    assert len(submissions) == 1


def test_burn_api_rejects_review_blocked_subtitles_with_conflict(burn_api):
    client, manager, root, _ = burn_api
    job_id = _write_deliverable_job(
        manager,
        root,
        delivery_status="review_required",
    )

    response = client.post(f"/api/jobs/{job_id}/exports/burned-video")

    assert response.status_code == 409
    assert "审校" in response.json()["detail"]


def test_burn_cancel_api_does_not_cancel_translation_job(burn_api):
    client, manager, root, _ = burn_api
    job_id = _write_deliverable_job(manager, root)
    assert client.post(
        f"/api/jobs/{job_id}/exports/burned-video"
    ).status_code == 202

    response = client.post(
        f"/api/jobs/{job_id}/exports/burned-video/cancel"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["delivery_status"] == "deliverable"
    assert payload["exports"]["burned_video"]["status"] == "cancelled"


def test_health_exposes_public_subtitle_burn_capability(burn_api, monkeypatch):
    client, _, _, _ = burn_api
    monkeypatch.setattr(
        app_module.subtitle_burner,
        "check_burn_capability",
        lambda: BurnCapability(
            available=False,
            error_code="font_missing",
            message="未找到可用的中文字体，无法生成带字幕视频",
            ffmpeg_path="private-ffmpeg-path",
            ffprobe_path="private-ffprobe-path",
            font=None,
        ),
    )

    health = client.get("/api/health").json()

    assert health["subtitle_burn"] == {
        "available": False,
        "error_code": "font_missing",
        "message": "未找到可用的中文字体，无法生成带字幕视频",
        "style": "black-outline-white-2-v1",
    }
    assert "private-ffmpeg-path" not in str(health)
    assert "private-ffprobe-path" not in str(health)


def test_start_translation_passes_explicit_burn_after_translation(burn_api, monkeypatch):
    client, manager, root, _ = burn_api
    job_id = _write_deliverable_job(manager, root)
    loaded = manager._load(job_id)
    loaded["status"] = "ready_for_translation"
    loaded["inputs"] = {
        "primary_srt": loaded["artifacts"]["final.zh.srt"],
        "secondary_srt": loaded["artifacts"]["final.zh.srt"],
    }
    loaded["options"].update({
        "model": "saved-model",
        "base_url": "https://api.example.test/v1",
    })
    manager._save(loaded)
    observed = {}

    def fake_start(job_id, secret, translation_options):
        observed.update(translation_options)
        return manager.get(job_id)

    monkeypatch.setattr(manager, "start_translation", fake_start)

    response = client.post(
        f"/api/jobs/{job_id}/start",
        data={
            "model": "saved-model",
            "base_url": "https://api.example.test/v1",
            "api_key": "secret",
            "burn_after_translation": "true",
        },
    )

    assert response.status_code == 200
    assert observed["burn_after_translation"] is True
