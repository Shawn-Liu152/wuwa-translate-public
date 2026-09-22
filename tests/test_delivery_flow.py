"""Delivery orchestration regressions; synthetic data, no external services."""
import json
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import app as app_module
from web import jobs as jobs_module
from web.private_cookie_store import PrivateCookieStore
from web.user_settings import UserSettingsStore


class EmptyCredentials:
    def read(self):
        return ""

    def write(self, value):
        pass

    def delete(self):
        pass


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)
    manager = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "private", legacy_jobs_root=root, enforce_os_acl=False,
    ))
    monkeypatch.setattr(app_module, "manager", manager)
    monkeypatch.setattr(app_module, "user_settings", UserSettingsStore(
        tmp_path / "settings.json", credential_store=EmptyCredentials(),
    ))
    calls = []
    monkeypatch.setattr(manager, "_submit_burn", lambda *args: calls.append(args))
    tokens = [secrets.token_urlsafe(32) for _ in range(4)]
    app_module.configure_local_access(
        session_token=tokens[0], bootstrap_token=tokens[1],
        csrf_token=tokens[2], launch_challenge=tokens[3],
    )
    with TestClient(app_module.app) as client:
        assert client.post("/api/session/bootstrap", headers={
            "origin": "http://testserver", "X-Subtitle-Bootstrap": tokens[1],
        }).status_code == 200
        client.headers["X-Subtitle-CSRF"] = tokens[2]
        folder = root / "delivery-test"
        folder.mkdir()
        (folder / "video.mp4").write_bytes(b"synthetic media")
        (folder / "final.zh.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8",
        )
        manager._save({
            "id": folder.name, "created_at": jobs_module.now(),
            "updated_at": jobs_module.now(), "status": "completed",
            "delivery_status": "review_required", "options": {}, "inputs": {},
            "artifacts": {"video": str(folder / "video.mp4"),
                          "final.zh.srt": str(folder / "final.zh.srt")},
            "logs": [], "generation": 0,
        })
        yield client, manager, folder, calls


def set_risks(manager, folder, states):
    items = [{"key": f"r-{i}", "subtitle_id": 1, "state": state,
              "translated": "Test", "review_required": True}
             for i, state in enumerate(states)]
    path = folder / "final.zh.risk-queue.json"
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    reviewed = folder / "final.reviewed.zh.srt"
    reviewed.write_bytes((folder / "final.zh.srt").read_bytes())
    job = manager._load(folder.name)
    job["artifacts"].update({path.name: str(path), reviewed.name: str(reviewed)})
    manager._save(job)


def test_last_save_clears_count_without_changing_delivery_semantics(delivery):
    client, manager, folder, calls = delivery
    set_risks(manager, folder, ["auto_resolved", "resolved", "review"])
    prefix = f"/api/jobs/{folder.name}"
    assert client.get(prefix).json()["review_pending_count"] == 1
    response = client.patch(prefix + "/risks/r-2", json={
        "state": "verified", "round2_text": "Test", "remember_for_future": False,
    })
    assert response.status_code == 200
    job = client.get(prefix).json()
    assert job["review_pending_count"] == 0
    assert job["burn_review_ready"] is True
    assert job["delivery_status"] == "review_required"
    assert client.post(prefix + "/exports/burned-video").status_code == 202
    assert len(calls) == 1
    assert "review_pending_count" not in manager._load(folder.name)


@pytest.mark.parametrize("state", ["pending", "review", "failed", "unknown"])
@pytest.mark.parametrize("delivery_status", ["review_required", "deliverable"])
def test_any_unfinished_required_item_blocks_video(delivery, state, delivery_status):
    client, manager, folder, calls = delivery
    set_risks(manager, folder, ["auto_resolved", "resolved", "verified", state])
    job = manager._load(folder.name)
    job["delivery_status"] = delivery_status
    manager._save(job)
    prefix = f"/api/jobs/{folder.name}"
    assert client.get(prefix).json()["burn_review_ready"] is False
    assert client.post(prefix + "/exports/burned-video").status_code == 409
    assert calls == []


def test_completed_video_reuse_and_changed_input_detection(delivery, monkeypatch):
    client, manager, folder, calls = delivery
    set_risks(manager, folder, ["auto_resolved", "resolved", "verified"])
    prefix = f"/api/jobs/{folder.name}"
    assert client.post(prefix + "/exports/burned-video").status_code == 202
    output = folder / "final.zh.burned.mp4"
    output.write_bytes(b"export")
    job = manager._load(folder.name)
    job["exports"]["burned_video"]["status"] = "completed"
    job["artifacts"][output.name] = str(output)
    manager._save(job)
    for _ in range(2):
        assert client.post(prefix + "/exports/burned-video").status_code == 200
    assert len(calls) == 1
    hashes = []
    original = manager._burn_input_fingerprint
    monkeypatch.setattr(manager, "_burn_input_fingerprint", lambda *args: (
        hashes.append(True) or original(*args)
    ))
    assert client.get(prefix).json()["burned_video_current"] is True
    assert client.get(prefix).json()["burned_video_current"] is True
    assert len(hashes) <= 1
    (folder / "final.reviewed.zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nChanged\n", encoding="utf-8",
    )
    assert client.get(prefix).json()["burned_video_current"] is False
    assert client.post(prefix + "/exports/burned-video").status_code == 202
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["pending", "queued", "running", "cancelled", "failed", "completed"])
def test_export_restores_from_disk_and_srt_is_independent(delivery, state):
    client, manager, folder, _ = delivery
    set_risks(manager, folder, ["verified"])
    job = manager._load(folder.name)
    job["exports"] = {"burned_video": {"status": state, "progress": 32}}
    manager._save(job)
    fresh = jobs_module.JobManager(PrivateCookieStore(
        folder.parent.parent / "second-private", legacy_jobs_root=folder.parent,
        enforce_os_acl=False,
    ))
    try:
        assert fresh.get(folder.name)["exports"]["burned_video"]["status"] == state
        fresh.recover_interrupted_jobs()
        expected = "failed" if state in {"queued", "running"} else state
        assert fresh.get(folder.name)["exports"]["burned_video"]["status"] == expected
    finally:
        fresh.close()
    response = client.get(f"/api/jobs/{folder.name}/artifacts/final.reviewed.zh.srt")
    assert response.status_code == 200
    assert b"Test" in response.content


def test_cancel_retry_uses_same_export_state(delivery):
    client, manager, folder, calls = delivery
    set_risks(manager, folder, ["verified"])
    path = f"/api/jobs/{folder.name}/exports/burned-video"
    assert client.post(path).json()["exports"]["burned_video"]["status"] == "queued"
    assert client.post(path + "/cancel").json()["exports"]["burned_video"]["status"] == "cancelled"
    assert client.post(path).json()["exports"]["burned_video"]["status"] == "queued"
    assert len(calls) == 2


def test_invalid_review_evidence_blocks_delivery_without_breaking_list(delivery):
    client, manager, folder, _ = delivery
    set_risks(manager, folder, ["verified"])
    (folder / "final.zh.risk-queue.json").write_text("invalid", encoding="utf-8")
    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert response.json()[0]["burn_review_ready"] is False
    assert client.post(f"/api/jobs/{folder.name}/exports/burned-video").status_code == 409


def test_open_folder_targets_job_and_returns_no_path(delivery, monkeypatch):
    client, _, folder, _ = delivery
    opened = []
    monkeypatch.setattr(app_module.os, "startfile", lambda path: opened.append(Path(path)), raising=False)
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda args: opened.append(Path(args[-1])))
    response = client.post(f"/api/jobs/{folder.name}/open-folder")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert opened == [folder.resolve()]
    assert client.post("/api/jobs/missing/open-folder").status_code == 404


def test_delivery_frontend_behavior():
    node = shutil.which("node")
    assert node, "Node is required for delivery frontend regression tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("delivery_frontend.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
