import asyncio
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from pipeline import safe_errors
from pipeline.parser.srt_parser import Subtitle
from pipeline.translate.prompt_builder import load_prompt_builder
from web import app as app_module
from web import jobs as jobs_module
from web.private_cookie_store import PrivateCookieStore
from web.user_settings import UserSettingsStore


SRT = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
JA_SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\nこれは日本語です\n\n"
    "2\n00:00:01,000 --> 00:00:02,000\nカルテジアが来た\n\n"
    "3\n00:00:02,000 --> 00:00:03,000\n本当に強いね\n"
).encode("utf-8")
KO_SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\n이것은 한국어입니다\n\n"
    "2\n00:00:01,000 --> 00:00:02,000\n카르테시아가 왔어요\n\n"
    "3\n00:00:02,000 --> 00:00:03,000\n정말 강하네요\n"
).encode("utf-8")
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class FakeCredentialStore:
    def __init__(self):
        self.secret = ""

    def read(self):
        return self.secret

    def write(self, secret):
        self.secret = secret

    def delete(self):
        self.secret = ""


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)
    manager = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    monkeypatch.setattr(manager, "_submit", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "manager", manager)
    credential_store = FakeCredentialStore()
    monkeypatch.setattr(
        app_module, "user_settings",
        UserSettingsStore(
            tmp_path / "settings.json",
            credential_store=credential_store,
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
        assert response.json()["csrf_token"] == csrf_token
        client.headers["X-Subtitle-CSRF"] = csrf_token
        yield client, manager, root


def _create_ready_job(client):
    response = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    )
    assert response.status_code == 200
    return response.json()


def _create_failed_formal_job(client, *, base_url="https://first.example/v1"):
    created = _create_ready_job(client)
    started = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "original-model",
            "api_key": "original-key",
            "base_url": base_url,
            "dry_run": "false",
        },
    )
    assert started.status_code == 200
    reset = client.post(f"/api/jobs/{created['id']}/force-reset")
    assert reset.status_code == 200
    assert reset.json()["status"] == "failed"
    return created["id"]


def test_root_serves_marketing_landing_page(api_client):
    client, _, _ = api_client

    response = client.get("/")

    assert response.status_code == 200
    assert "把鸣潮游戏视频字幕" in response.text
    assert 'href="/app"' in response.text
    assert "phoebe-glasses-cover.jpg" in response.text
    assert "/static/pet.js" in response.text
    assert "b站：加藤惠soft" in response.text
    assert 'translate="no">b站：加藤惠soft' in response.text
    assert 'href="#proof"' not in response.text
    assert "解决什么？" not in response.text
    assert "不是“能翻译”" not in response.text
    assert "适合谁用？" not in response.text


def test_app_route_keeps_existing_translation_workbench(api_client):
    client, _, _ = api_client

    response = client.get("/app")

    assert response.status_code == 200
    assert "创建翻译任务" in response.text
    assert 'id="url-form"' in response.text
    assert "/static/pet.js" in response.text
    assert "b站：加藤惠soft" in response.text
    assert 'translate="no">b站：加藤惠soft' in response.text


def test_local_web_privacy_boundary(api_client):
    client, _, _ = api_client

    response = client.get("/")
    assert response.headers["content-security-policy"].startswith(
        "default-src 'self'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400

    rejected = client.post(
        "/api/jobs/batch-archive",
        headers={"origin": "https://attacker.example"},
        json={},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "拒绝跨来源操作"
    accepted = client.post(
        "/api/jobs/batch-archive",
        headers={"origin": "http://testserver"},
        json={},
    )
    assert accepted.status_code == 422


def test_api_rejects_oversized_request_body_before_route(api_client, monkeypatch):
    client, _, _ = api_client
    monkeypatch.setattr(app_module, "MAX_REQUEST_BODY_BYTES", 8)

    response = client.post(
        "/api/jobs/batch-archive",
        content=b"123456789",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "请求体超过本机安全上限"


def test_chunked_request_body_limit_stops_before_route_parsing(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_REQUEST_BODY_BYTES", 8)
    messages = iter((
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789", "more_body": False},
    ))

    async def receive():
        return next(messages)

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/jobs",
        "raw_path": b"/api/jobs",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }, receive)

    async def call_next(_request):
        await request.body()
        return Response("route parsed")

    response = asyncio.run(
        app_module.enforce_request_body_limit(request, call_next)
    )
    assert response.status_code == 413
    assert json.loads(response.body.decode("utf-8"))["detail"] == (
        "请求体超过本机安全上限"
    )


def test_sse_connection_quota_is_bounded_and_reusable(monkeypatch):
    monkeypatch.setattr(app_module, "MAX_SSE_CONNECTIONS", 1)
    with app_module._sse_lock:
        app_module._sse_connections = 0

    assert app_module._acquire_sse_slot() is True
    assert app_module._acquire_sse_slot() is False
    app_module._release_sse_slot()
    assert app_module._acquire_sse_slot() is True
    app_module._release_sse_slot()


def test_local_api_requires_single_use_launcher_session(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_module, "JOBS_ROOT", root)
    manager = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    monkeypatch.setattr(app_module, "manager", manager)
    session_token = secrets.token_urlsafe(32)
    bootstrap_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    app_module.configure_local_access(
        session_token=session_token,
        bootstrap_token=bootstrap_token,
        csrf_token=csrf_token,
        launch_challenge=secrets.token_urlsafe(32),
    )

    with TestClient(app_module.app) as unauthenticated:
        assert unauthenticated.get("/").status_code == 200
        assert unauthenticated.get("/app").status_code == 200
        assert unauthenticated.get("/api/health").status_code == 200
        rejected = unauthenticated.get("/api/jobs")
        assert rejected.status_code == 401
        assert rejected.headers["content-security-policy"].startswith(
            "default-src 'self'"
        )
        wrong = unauthenticated.post(
            "/api/session/bootstrap",
            headers={"X-Subtitle-Bootstrap": secrets.token_urlsafe(32)},
        )
        assert wrong.status_code == 401

        accepted = unauthenticated.post(
            "/api/session/bootstrap",
            headers={"X-Subtitle-Bootstrap": bootstrap_token},
        )
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json() == {"ok": True, "csrf_token": csrf_token}
        cookie = accepted.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert session_token not in accepted.text
        assert bootstrap_token not in accepted.text
        status = unauthenticated.get("/api/session")
        assert status.status_code == 200
        assert status.json()["csrf_token"] == csrf_token

    # Use a fresh manager because the first TestClient closed its runtime.
    replay_manager = jobs_module.JobManager(PrivateCookieStore(
        tmp_path / "replay-private-cookies",
        legacy_jobs_root=root,
        enforce_os_acl=False,
    ))
    monkeypatch.setattr(app_module, "manager", replay_manager)
    with TestClient(app_module.app) as replay:
        repeated = replay.post(
            "/api/session/bootstrap",
            headers={"X-Subtitle-Bootstrap": bootstrap_token},
        )
        assert repeated.status_code == 401


def test_local_api_rejects_missing_csrf_and_cross_site_fetch(api_client):
    client, _, _ = api_client
    csrf_token = client.headers.pop("X-Subtitle-CSRF")
    missing = client.post("/api/jobs/batch-archive", json={"ids": []})
    assert missing.status_code == 403
    client.headers["X-Subtitle-CSRF"] = csrf_token

    cross_site = client.get(
        "/api/jobs",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["detail"] == "拒绝跨站请求"


def test_retired_browser_review_surface_stays_unavailable(api_client):
    client, _, _ = api_client

    assert client.get("/review/english").status_code == 404
    assert client.get("/api/review-batch/english").status_code == 404
    assert client.post(
        "/api/review-batch/english/review", json={"reviewer": "private"},
    ).status_code == 404


def test_capcut_launcher_discovers_current_users_install(tmp_path, monkeypatch):
    executable = tmp_path / "JianyingPro" / "Apps" / "JianyingPro.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"launcher")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("SUBTITLE_CAPCUT_EXECUTABLE", raising=False)

    assert app_module._find_capcut_executable() == executable.resolve()


def test_capcut_launcher_endpoint_starts_only_discovered_app(
        api_client, tmp_path, monkeypatch):
    client, _, _ = api_client
    executable = tmp_path / "CapCut.exe"
    executable.write_bytes(b"launcher")
    launches = []
    monkeypatch.setattr(
        app_module, "_find_capcut_executable", lambda: executable
    )
    monkeypatch.setattr(
        app_module.subprocess, "Popen",
        lambda command, **options: launches.append((command, options)),
    )

    response = client.post("/api/apps/capcut/open")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert launches[0][0] == [str(executable)]
    assert launches[0][1]["cwd"] == str(executable.parent)


def test_workbench_has_persistent_settings_page():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    pet_script = (WEB_ROOT / "static" / "pet.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'data-view="settings"' in page
    assert 'id="settings"' in page
    assert 'id="app-settings-form"' in page
    assert "APP_SETTINGS_KEY" in app_script
    assert "saveAppSettings" in app_script
    assert "phoebe-pet-settings" in app_script
    assert "phoebe-pet-settings" in pet_script
    assert ".settings-dashboard" in styles


def test_url_view_is_allowlisted_before_navigation_dom_lookup():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "const NAVIGATION_VIEWS = new Set([" in app_script
    for view in (
        "workbench", "review", "glossary", "prompt", "settings", "guide", "faq",
    ):
        assert f"'{view}'" in app_script
    assert "function normalizeNavigationView" in app_script
    assert "normalizeNavigationView(params.get('view'))" in app_script
    assert 'document.querySelector(`.nav[data-view="${view}"]`)' not in app_script


def test_api_key_uses_backend_secure_storage_without_browser_persistence():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    core_script = (WEB_ROOT / "static" / "workbench-core.js").read_text(encoding="utf-8")

    assert "pageApiKey" not in app_script
    assert "sessionStorage" not in app_script
    assert 'form.delete("api_key")' in app_script
    assert 'api("/api/user-settings"' in app_script
    assert "Windows Credential Manager" in page
    assert "不会回显原文" in page
    assert "secret-dialog" not in page
    assert "requestSecret" not in core_script


def test_workbench_hides_dry_run_and_recovers_placeholder_tasks():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'name="dry_run"' not in app_script
    assert "completedDryRun" in app_script
    assert "开始正式翻译" in app_script
    assert "试运行，没有调用翻译模型" in app_script
    assert "为什么字幕全是“[待翻译]”" in page


def test_workbench_documents_person_name_review_and_whisper_permissions():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert "为什么译对的人名也出现在人工审校里？" in page
    assert "为什么辅助英文已经识别出人名，却没有进入人工审校？" in page
    assert "所有有效英文证据" in page
    assert "WinError 5：拒绝访问" in page
    assert "确实译错或疑似未知人名时才调用 Round 2" in page


def test_workbench_keeps_source_language_copy_and_upload_validation_in_sync():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="flow-source-evidence"' in page
    assert "补充源语言证据" in page
    assert "已有两份源语言 SRT？直接上传" in page
    assert "第二路源语言证据" in page
    assert "日语或韩语任务提示源语言不匹配" in page
    assert "<b>现象：</b>" in page
    assert "<b>原因：</b>" in page
    assert "<b>操作步骤：</b>" in page
    assert "${sourceLanguageLabel(job.options?.source_language || 'en')} → 简体中文" in app_script


def test_waiting_capcut_flow_can_launch_desktop_app():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert "打开剪映" in app_script
    assert "openCapcutApp" in app_script
    assert "/api/apps/capcut/open" in app_script
    assert ".capcut-launch" in styles
    assert "找不到剪映" in page
    assert "SUBTITLE_CAPCUT_EXECUTABLE" in page


def test_workbench_blocks_primary_delivery_until_required_review_is_clear():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "为什么自动字幕已生成，主按钮却要求先审校？" in page
    assert "自动流程完成不等于可以安全交付" in page
    assert "处理必审风险" in app_script
    assert "下载自动版字幕（尚未完成审校）" in app_script
    assert "下载可交付中文字幕" in app_script
    assert "unresolvedReviewRequiredCount" in app_script


def test_risk_review_has_persistent_task_context_and_task_picker():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'id="review-task-context"' in page
    assert 'id="review-task-select"' in page
    assert "怎样在风险审校页切换任务？" in page
    assert "不必回到工作台" in page
    assert "renderReviewTaskContext" in app_script
    assert "reviewableJobs" in app_script
    assert ".review-task-context" in styles


def test_workbench_uses_non_color_status_cues_for_errors_reviews_and_deletions():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert "为什么有些提示是深蓝色并带黄色边框？" in page
    assert "不能只依赖颜色区分" in page
    assert "STATUS_MARKS" in app_script
    assert "toast-${normalizedKind}" in app_script
    assert ".status-mark" in styles
    assert ".toast-error" in styles
    assert ".job-actions .danger::before" in styles


def test_workbench_keeps_keyboard_context_for_views_and_risk_cards():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'id="view-announcer"' in page
    assert "键盘怎样更快审校风险？" in page
    assert "function focusViewHeading" in app_script
    assert "function announceView" in app_script
    assert 'tabindex="0"' in app_script
    assert "event.key === 'ArrowDown'" in app_script
    assert ".risk:focus-visible" in styles
    assert "safe-area-inset-bottom" in styles


def test_workbench_preserves_visual_identity_without_template_motion():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert "app.css?v=20260923-ui-fixes-css25" in page
    assert "--ease-authored: cubic-bezier(.16, 1, .3, 1)" in styles
    assert "animation: phoebe-float 7s ease-in-out infinite" in styles
    assert "animation: toast-in .22s var(--ease-authored)" in styles
    assert "Calm operating refinement" not in styles


def test_workbench_explains_context_memory_and_requires_explicit_reuse():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "app.js?v=20260923-ui-fixes-v32" in page
    assert "生成交付文件" in page
    assert 'id="session-recovery"' in page
    assert '会话已失效' in page
    assert '不会自动重放' in page
    assert 'workbench-core.js?v=20260921-core4' in page
    assert "打开当前任务目录" in page
    assert "为什么跨好几条字幕的半句话更容易译顺了？" in page
    assert "前后六条局部证据、最近十二条对话记忆" in page
    assert "人工修改会不会自动影响以后的字幕？" in page
    assert "以后遇到完全相同的英文时，优先沿用这版" in page
    assert "data-memory-approve" in app_script
    assert "remember_for_future" in app_script


def test_workbench_explains_technical_file_organization_without_deletion():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "整理过程文件" in app_script
    assert "只保留最终字幕、已审校字幕、视频和封面" in app_script
    assert "英文来源、Whisper、Round 1/2、断点、风险记录、配置和证据目录" in app_script
    assert "过程文件" in app_script
    assert "怎么把下载目录整理得更清爽？" in page
    assert "final.reviewed.zh.srt" in page
    assert "视频和封面" in page


def test_workbench_explains_speech_timing_alignment():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert "为什么字幕和说话时间对不上？" in page
    assert "保留 YouTube 英文文本" in page
    assert "采用 Whisper 的语音时间" in page
    assert "重新创建任务" in page


def test_workbench_explains_safe_http_400_recovery():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    assert "HTTP 400" in page
    assert "不会保存或显示上游原始错误正文" in page
    assert "确认页面显示的目标主机正确" in page
    assert "继续失败批次" in page


def test_workbench_maps_every_persisted_job_stage_to_chinese():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    expected_stages = {
        "Queued": "任务已排队",
            "Sources ready": "源语言证据已就绪",
        "Downloading YouTube assets": "正在获取 YouTube 素材",
            "Waiting for CapCut English SRT": "等待上传剪映主字幕",
        "Translation queued": "翻译任务已排队",
            "Preparing English sources": "正在整理源语言证据",
        "Completed": "翻译与风险分析已完成",
        "Failed": "处理失败",
        "Download failed": "素材获取失败",
        "API authentication failed": "API Key 验证失败",
        "API request failed": "模型请求失败",
        "Cancelled by user": "任务已取消",
        "Force reset (was stuck)": "任务已强制停止",
        "Download retry queued": "素材重试已排队",
        "Ready for translation (retry)": "可继续翻译",
        "Ready for formal translation": "等待正式翻译",
            "Source strategy upgraded": "已启用补充源语言证据",
        "Paused by user": "任务已暂停",
        "Paused — API key required to resume": "已暂停，等待重新输入 API Key",
        "Interrupted by service restart": "服务重启已中断任务",
    }

    for stage, label in expected_stages.items():
        assert f"'{stage}': '{label}'" in app_script


def test_workbench_documents_model_pricing_scope_without_recommendations():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "页面上的模型费用是怎样估算的？" in page
    assert "按未命中缓存保守计算" in page
    assert "实际扣费仍以接口供应商账单为准" in page
    assert "deepseek-v4" not in page
    assert "官方价估算" in app_script
    assert "暂不估算" in app_script


def test_job_metrics_attach_deepseek_official_price_estimate(api_client):
    _, manager, root = api_client
    job_id = "pricing-job"
    workspace = root / job_id
    workspace.mkdir()
    (workspace / "final.zh.pipeline.metrics.json").write_text(
        json.dumps(
            {
                "round1": {"tokens_in": 1_000_000, "tokens_out": 500_000},
                "round2": {"tokens_in": 100_000, "tokens_out": 50_000},
            }
        ),
        encoding="utf-8",
    )
    job = {
        "id": job_id,
        "created_at": jobs_module.now(),
        "updated_at": jobs_module.now(),
        "status": "completed",
        "stage": "Completed",
        "workspace": str(workspace),
        "inputs": {},
        "options": {
            "model": "deepseek-v4-flash",
            "round2_model": "gpt-5.6-luna",
        },
        "artifacts": {},
        "logs": [],
        "error": None,
    }
    manager._save(job)

    metrics = manager._collect_available_metrics(job_id, job)

    assert metrics["round1"]["pricing"]["amount"] == 2.0
    assert metrics["round1"]["pricing"]["currency"] == "CNY"
    assert metrics["round2"]["pricing"]["estimable"] is False


def test_workbench_exposes_youtube_cookie_authentication():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="youtube-auth"' in page
    assert 'value="chrome"' in page
    assert 'value="file"' in page
    assert '<option value="edge"' not in page
    assert '<option value="firefox"' not in page
    assert 'name="youtube_cookies_text"' in page
    assert 'id="paste-youtube-cookies"' in page
    assert "syncYoutubeAuth" in app_script
    assert "pasteInitialYoutubeCookies" in app_script
    assert "renderYoutubeRecovery" in app_script
    assert "retryDownloadWithAuth" in app_script
    assert 'name="youtube_cookies_text"' in app_script
    assert "pasteYoutubeCookies" in app_script
    assert "payload.delete('youtube_cookies_text')" in app_script
    assert "form?.delete('youtube_cookies_text')" in app_script


def test_workbench_has_searchable_usage_and_faq_pages():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'data-view="guide"' in page
    assert 'id="guide"' in page
    assert 'data-view="faq"' in page
    assert 'id="faq"' in page
    assert 'id="faq-search"' in page
    assert page.count('class="faq-item"') >= 14
    # 2026-09-03：+“接口地址不再预填 DeepSeek”（FE-P1-04）、
    # +“启动后会话无效”（APP-AUTH-001 syncUrl 保留 hash 修复）
    # 2026-09-03 E-1：+6 条错误码处理办法（authentication_error /
    # configuration_error / provider_unavailable / translation_batch /
    # source_unreliable / job_state），并把过期的“52 个常见问题”占位改为真实条数
    # 2026-09-04：+3 条下载韧性提示（自动降级 / 会话不匹配匿名重试 / ffmpeg 本地合并）
    # 2026-09-06 P1/P2：+2 条（未翻译英文句说明 / 补充第二源操作）；quota 条目改写覆盖订阅限额（429 GoUsageLimitError）
    assert page.count('class="faq-item"') == 66
    assert "66 个常见问题" in page
    assert "命令行试运行异常" in page
    assert "UnicodeEncodeError" in page
    assert "跨盘上传失败" in page
    assert "WinError 17" in page
    assert "批量重试翻译任务前也需先保存 Key" in page
    assert "当前清晰度不可用，已自动降级重试" in page
    assert "YouTube 会话不匹配，已改用匿名方式重试" in page
    assert "yt-dlp 合并异常，已改用 ffmpeg 本地合并" in page
    assert "为什么旧书签打不开，或者页面提示工作台会话无效？" in page
    assert "每次启动都会先独占一个随机本机端口" in page
    assert "排队时正文只在进程内存" in page
    assert "关闭继承权限的本地临时目录" in page
    assert "远程接口只允许 HTTPS" in page
    assert "不会保存或显示上游原始错误正文" in page
    assert 'required id="url-base-url" name="base_url"' in page
    assert 'required id="url-model" name="model"' in page
    assert "/api/user-settings" in app_script
    assert 'id="faq-japanese"' in page
    assert "日语 → 简体中文" in page
    assert page.count("<b>现象：</b>") >= 5
    assert "日语任务只接受日语人工/自动字幕或日语 Whisper 证据" in page
    assert "正确名字本身不是付费 Round 2 的理由" in page
    assert "日语字幕里的假名人名或地名没有命中术语库怎么办？" in page
    assert "同一个日语专名会同时收录汉字表记和假名读音" in page
    assert "低置信中文候选" in page
    assert "仍会留在“待我处理”" in page
    assert "不会标成已解决" in page
    assert "实体风险没有被实际修正" in page
    assert "相邻字幕会作为只读语境交给 Reviewer" in page
    assert 'id="source-language"' in page
    assert 'id="prompt-source-language"' in page
    assert 'data-help-action="download-options"' in page
    assert "创建任务的表单在哪里？" in page
    assert "点击顶部或空状态中的“新建翻译任务”" in page
    assert "为什么一句话会只剩半句或几个字" in page
    assert "有效内容会先合并到相邻字幕" in page
    assert "模型连续漏译字幕" in page
    assert "为什么普通英文单词会触发“模型连续漏译字幕”？" in page
    assert "重新运行同一任务即可复用已经完成的批次" in page
    assert "模型一直显示处理中，但没有返回译文怎么办？" in page
    assert "LLM_STREAM_DEADLINE_SECONDS" in page
    assert "只重试当前失败批次" in page
    assert "300 秒" in page
    assert "缺少 yt_dlp 模块" in page
    assert "install_web.bat" in page
    assert "为什么不再生成浏览器真人审核包和指标？" in page
    assert "应用不会再收集审核者姓名" in page
    assert "旧的英语质量审核地址为什么打不开？" in page
    assert "旧的审核 API 会返回 404" in page
    assert 'href="/review/english"' not in page
    assert "review-batch-callout" not in page
    assert ".review-batch-callout" not in styles
    assert "无关重写" in page
    assert "旧任务" in page and "重新运行" in page
    assert "任务详情里的五个标签怎样切换？" in page
    assert "按左、右方向键切换相邻标签" in page
    assert "按 <kbd>Home</kbd> 跳到 Overview" in page
    assert "filterFaqItems" in app_script
    assert "runHelpAction" in app_script
    assert "syncSourceLanguageCopy" in app_script
    assert "primary_evidence" in app_script
    assert ".manual-timeline" in styles
    assert ".faq-item" in styles


def test_failed_job_error_banner_matches_public_error_contract():
    """E-1：失败横幅必须与后端公共错误码契约保持一致，且跳转锚点真实存在。"""
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    guidance_block = app_script.split("const ERROR_GUIDANCE = {", 1)[1].split(
        "\n};", 1
    )[0]
    guided = set(re.findall(r"^ {2}([a-z0-9_]+): \{", guidance_block, re.M))
    assert guided == set(safe_errors._PUBLIC_MESSAGES)

    anchors = set(re.findall(r'data-faq-code="([^"]+)"', page))
    referenced = set(re.findall(r"faqCode: '([^']+)'", guidance_block))
    assert referenced and referenced <= anchors

    views_block = app_script.split("const NAVIGATION_VIEWS = new Set([", 1)[1].split(
        "]);", 1
    )[0]
    navigation = set(re.findall(r"'([a-z]+)'", views_block))
    assert set(re.findall(r"actionView: '([^']+)'", guidance_block)) <= navigation

    # 横幅是卡片 <button> 的兄弟节点——HTML 不允许 button 嵌套 button。
    card = app_script.split('<div class="job-wrap"', 1)[1].split("</div>`;", 1)[0]
    assert card.index("</button>") < card.index("renderErrorBanner(job)")
    assert "renderErrorBanner(job)" in app_script.split("jobActions = ", 1)[1]

    # 原因文案只能取后端已脱敏的 job.error，前端不得自行拼接上游正文。
    assert "String(job.error || '')" in app_script
    assert "job.error_code" in app_script
    assert "job.upstream_status" in app_script
    assert "Number.isInteger(upstreamStatus)" in app_script
    for token in (
        "guidance.code",
        "guidance.reason",
        "guidance.action",
        "guidance.actionView",
        "guidance.faqCode",
    ):
        assert f"escapeHtml({token})" in app_script

    # 委托而非直接绑定：横幅随任务列表和详情重绘动态生成。
    assert "openFaqForCode" in app_script
    assert "'.job-error-faq[data-faq-code]'" in app_script

    for selector in (
        ".job-error-banner",
        ".job-error-code",
        ".job-error-status",
        ".job-error-reason",
        ".job-error-action",
        ".job-error-faq",
        ".faq-item-flash",
    ):
        assert selector in styles

    assert re.search(r"/static/app\.js\?v=[0-9a-z-]+", page)
    assert re.search(r"/static/app\.css\?v=[0-9a-z-]+", page)


def test_web_pet_library_contains_all_four_selectable_pets():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    pet_script = (WEB_ROOT / "static" / "pet.js").read_text(encoding="utf-8")
    asset_root = WEB_ROOT / "static" / "assets" / "pets"
    thumbnail_root = asset_root / "thumbs"

    for pet_id in ("feibi", "coletta", "phrolova", "taffy"):
        assert f'value="{pet_id}"' in page
        assert pet_id in pet_script
        assert f"/static/assets/pets/thumbs/{pet_id}.webp" in page
        thumbnail = thumbnail_root / f"{pet_id}.webp"
        assert thumbnail.exists()
        assert thumbnail.stat().st_size < 100_000
    assert "pet_id" in app_script
    assert "PET_CATALOG" in pet_script
    assert (asset_root / "coletta.webp").exists()
    assert (asset_root / "phrolova.webp").exists()
    assert (asset_root / "taffy.webp").exists()


def test_sse_progress_updates_detail_in_place():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    server_script = (WEB_ROOT / "app.py").read_text(encoding="utf-8")
    progress_handler = app_script.split("es.addEventListener('progress'", 1)[1].split(
        "es.addEventListener('done'", 1
    )[0]

    assert "updateProgressView(payload)" in progress_handler
    assert "renderDetail()" not in progress_handler
    assert "live-status-progress-slot" in app_script
    assert "'elapsed_seconds': job.get('elapsed_seconds', 0)" in server_script


def test_pet_renderer_uses_canvas_and_real_task_states():
    pet_script = (WEB_ROOT / "static" / "pet.js").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'document.createElement("canvas")' in pet_script
    assert "imageSmoothingEnabled = false" in pet_script
    assert "FRAME_WIDTH = 192" in pet_script
    assert "FRAME_HEIGHT = 208" in pet_script
    assert "LOOP_MS = 1100" in pet_script
    assert "phoebe-pet" in app_script


def test_animated_header_and_pet_avoid_scroll_time_render_loops():
    pet_script = (WEB_ROOT / "static" / "pet.js").read_text(encoding="utf-8")
    pet_styles = (WEB_ROOT / "static" / "pet.css").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    app_styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'document.addEventListener("visibilitychange"' in pet_script
    assert "frameTimer = window.setTimeout" in pet_script
    assert "fitToolsToViewport" in pet_script
    assert "--web-pet-tools-shift" in pet_styles
    assert "filter: drop-shadow" not in pet_styles
    assert "COMPACT_ENTER_Y" in app_script
    assert "COMPACT_EXIT_Y" in app_script
    assert ".header-compact .page-header::after" in app_styles
    assert ".header-compact .jiubi-sticker img" in app_styles


def test_health_reports_optional_capabilities(api_client, monkeypatch):
    client, _, _ = api_client
    app_module._module_imports.cache_clear()
    monkeypatch.setattr(
        app_module.importlib.util,
        "find_spec",
        lambda name: object() if name == "yt_dlp" else None,
    )
    monkeypatch.setattr(
        app_module.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )
    monkeypatch.setattr(app_module.shutil, "which", lambda name: None)

    response = client.get("/api/health")

    assert response.status_code == 200
    health = response.json()
    assert health["ok"] is True
    assert health["version"] == app_module.PROJECT_VERSION
    assert health["distribution"] == "source"
    assert "jobs_root" not in health
    assert health["capabilities"] == {
        "youtube_download": True,
        "local_whisper": False,
        "ffmpeg": False,
    }
    assert "本地 Whisper 未安装" in health["warnings"]
    assert "FFmpeg 未安装（视频下载不可用）" in health["warnings"]


def _write_health_job(
    manager, root, job_id: str, *, status: str,
    error_code: str | None = None, minutes_ago: int = 0,
):
    """Persist a job record straight to disk so health has something to report."""
    workspace = root / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = (
        datetime.now().astimezone() - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")
    job = {
        "id": job_id,
        "created_at": stamp,
        "updated_at": stamp,
        "status": status,
        "stage": "Test",
        "progress": {"completed": 0, "total": 0},
        "inputs": {},
        "options": {"dry_run": True, "model": "test"},
        "generation": 1,
        "workspace": str(workspace),
        "artifacts": {},
        "logs": [],
        # 上游原文只允许以公开错误码的形式离开磁盘。
        "error": "UPSTREAM RAW TEXT MUST NOT LEAK" if error_code else None,
        "error_code": error_code,
    }
    manager._save(job)
    return job


def test_health_reports_stuck_jobs_failures_and_disk(api_client, monkeypatch):
    client, manager, root = api_client
    app_module._module_imports.cache_clear()
    monkeypatch.setattr(app_module.shutil, "which", lambda name: None)
    _write_health_job(
        manager, root, "failedjob1", status="failed", error_code="quota_error",
    )
    _write_health_job(manager, root, "stuckjob1", status="running", minutes_ago=90)

    health = client.get("/api/health").json()

    assert [entry["job_id"] for entry in health["stuck_jobs"]] == ["stuckjob1"]
    assert health["stuck_jobs"][0]["worker_alive"] is False
    assert health["stuck_jobs"][0]["silent_minutes"] >= 90
    assert health["recent_failures"] == ["quota_error"]
    assert health["disk"]["limit_bytes"] == jobs_module.MAX_JOB_DISK_BYTES
    assert health["disk"]["used_bytes"] >= 0
    assert health["disk"]["percent"] >= 0.0
    assert "ffmpeg" in health["install_hints"]


def test_health_diagnostics_never_leak_paths_or_upstream_text(api_client):
    client, manager, root = api_client
    _write_health_job(
        manager, root, "failedjob1", status="failed", error_code="quota_error",
    )
    _write_health_job(manager, root, "stuckjob1", status="running", minutes_ago=90)

    payload = json.dumps(client.get("/api/health").json(), ensure_ascii=False)

    assert "UPSTREAM RAW TEXT MUST NOT LEAK" not in payload
    assert str(root) not in payload
    assert "jobs_root" not in payload


def test_health_normalizes_unknown_persisted_error_codes(api_client):
    client, manager, root = api_client
    _write_health_job(
        manager, root, "failedjob1", status="failed",
        error_code="quota_error",
    )
    # Simulate a legacy/hand-edited job record that bypassed _save().
    record_path = manager._path("failedjob1")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["error_code"] = "unknown_provider_detail"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    health = client.get("/api/health").json()

    assert health["recent_failures"] == ["internal_error"]


def test_health_install_hints_stay_empty_when_ffmpeg_exists(api_client, monkeypatch):
    client, _, _ = api_client
    monkeypatch.setattr(
        app_module.shutil, "which", lambda name: "/usr/bin/ffmpeg",
    )

    health = client.get("/api/health").json()

    assert health["capabilities"]["ffmpeg"] is True
    assert health["install_hints"] == {}


def test_stuck_job_watchdog_thread_stops_on_shutdown(api_client, monkeypatch):
    """看门狗线程必须能被优雅停止，否则关闭工作台时会挂住。"""
    _, manager, _ = api_client
    monkeypatch.setattr(app_module, "STUCK_JOB_CHECK_INTERVAL_SECONDS", 0.01)
    calls = []
    monkeypatch.setattr(manager, "reset_stuck_jobs", lambda: calls.append(1) or [])
    stop = threading.Event()
    thread = threading.Thread(
        target=app_module._run_stuck_job_watchdog, args=(stop,), daemon=True,
    )

    thread.start()
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)

    assert calls, "watchdog never scanned"
    assert not thread.is_alive()


def test_portable_capability_limits_are_explained_in_ui():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "便携版提示没有 FFmpeg 或 Whisper，还能翻译吗" in page
    assert "VTT 字幕由工作台在本机转换为 SRT，不需要 FFmpeg" in page
    assert "可以直接上传已有 SRT" in page
    assert "function applyCapabilityLimits(health)" in app_script
    assert "videoToggle.disabled = true" in app_script
    assert "option.disabled = option.value !== 'youtube'" in app_script


def test_proxy_credentials_are_rejected():
    with pytest.raises(app_module.HTTPException) as error:
        app_module._validate_proxy("http://private-user:private-pass@127.0.0.1:7890")

    assert error.value.status_code == 422
    assert "用户名或密码" in error.value.detail


def test_direct_upload_prepares_sources_without_translation_secret(api_client):
    client, manager, root = api_client
    response = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "ready_for_translation"
    assert job["options"]["model"] == ""
    assert job["options"]["api_key_configured"] is False
    assert job["id"] not in manager.secrets


def test_api_persists_explicit_japanese_source_language(api_client):
    client, _, _ = api_client

    response = client.post(
        "/api/jobs",
        data={"source_language": "ja", "target_language": "zh-CN"},
        files={
            "capcut_en": ("capcut.srt", JA_SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", JA_SRT, "application/x-subrip"),
        },
    )

    assert response.status_code == 200
    assert response.json()["options"]["source_language"] == "ja"


def test_api_accepts_language_neutral_japanese_source_fields(api_client):
    client, _, _ = api_client
    response = client.post(
        "/api/jobs",
        data={"source_language": "ja", "target_language": "zh-CN"},
        files={
            "primary_srt": ("primary.ja.srt", JA_SRT, "application/x-subrip"),
            "secondary_srt": ("secondary.ja.srt", JA_SRT, "application/x-subrip"),
        },
    )

    assert response.status_code == 200
    assert response.json()["inputs"]["primary_srt"].endswith("capcut.ja.srt")
    assert response.json()["inputs"]["secondary_srt"].endswith("whisper.ja.srt")
    assert response.json()["options"]["target_language"] == "zh-CN"


def test_api_accepts_language_neutral_korean_source_fields(api_client):
    client, _, _ = api_client
    response = client.post(
        "/api/jobs",
        data={"source_language": "ko", "target_language": "zh-CN"},
        files={
            "primary_srt": ("primary.ko.srt", KO_SRT, "application/x-subrip"),
            "secondary_srt": ("secondary.ko.srt", KO_SRT, "application/x-subrip"),
        },
    )

    assert response.status_code == 200
    assert response.json()["inputs"]["primary_srt"].endswith("capcut.ko.srt")
    assert response.json()["options"]["source_language"] == "ko"


@pytest.mark.parametrize("source_language,target_language", [
    ("fr", "zh-CN"), ("en", "zh-TW"),
])
def test_api_rejects_unsupported_language_pairs(
        api_client, source_language, target_language):
    client, _, _ = api_client

    response = client.post(
        "/api/jobs",
        data={
            "source_language": source_language,
            "target_language": target_language,
        },
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    )

    assert response.status_code == 422


def test_api_rejects_invalid_batch_only_when_translation_starts(api_client):
    client, manager, _ = api_client
    created = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    )
    job_id = created.json()["id"]
    response = client.post(
        f"/api/jobs/{job_id}/start",
        data={"model": "test", "dry_run": "true", "batch_size": "0"},
    )

    assert response.status_code == 422
    assert manager.get(job_id)["status"] == "ready_for_translation"


def test_formal_translation_requires_explicit_endpoint_before_mutating_job(
        api_client):
    client, manager, _ = api_client
    created = _create_ready_job(client)

    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "test-model", "api_key": "top-secret",
            "dry_run": "false",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "API 接口地址不能为空"
    assert manager.get(created["id"])["status"] == "ready_for_translation"
    assert created["id"] not in manager.secrets


def test_dry_run_allows_empty_endpoint(api_client):
    client, _, _ = api_client
    created = _create_ready_job(client)

    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={"model": "test-model", "dry_run": "true"},
    )

    assert response.status_code == 200
    assert response.json()["options"]["base_url"] == ""


def test_job_api_returns_only_task_relative_paths_and_open_folder_ack(
        api_client, monkeypatch):
    client, manager, root = api_client
    created = _create_ready_job(client)

    assert "workspace" not in created
    assert not Path(created["inputs"]["capcut_en"]).is_absolute()
    assert not Path(created["inputs"]["whisper_en"]).is_absolute()
    raw = (root / created["id"] / "job.json").read_text(encoding="utf-8")
    assert str(root) not in raw

    if hasattr(app_module.os, "startfile"):
        monkeypatch.setattr(app_module.os, "startfile", lambda path: None)
    opened = client.post("/api/downloads/open")
    assert opened.status_code == 200
    assert opened.json() == {"ok": True}

    detail = client.get(f"/api/jobs/{created['id']}").json()
    assert "workspace" not in detail
    assert all(
        not Path(value).is_absolute()
        for value in detail.get("artifacts", {}).values()
        if isinstance(value, str) and value
    )


def test_formal_translation_rejects_remote_plain_http_endpoint(api_client):
    client, manager, _ = api_client
    created = _create_ready_job(client)

    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "test-model", "api_key": "top-secret",
            "base_url": "http://api.example.com/v1", "dry_run": "false",
        },
    )

    assert response.status_code == 422
    assert "远程 API 接口必须使用 HTTPS" in response.json()["detail"]
    assert manager.get(created["id"])["status"] == "ready_for_translation"
    assert created["id"] not in manager.secrets


def test_start_translation_accepts_shared_settings_without_persisting_key(api_client):
    client, manager, root = api_client
    created = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    ).json()

    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "round-one-model", "round2_model": "round-two-model",
            "api_key": "top-secret",
            "base_url": "https://api.example.com/v1", "batch_size": "12",
            "game": "wuwa", "local_whisper": "false", "dry_run": "false",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "queued"
    assert job["options"]["model"] == "round-one-model"
    assert job["options"]["round2_model"] == "round-two-model"
    assert job["options"]["base_url"] == "https://api.example.com/v1"
    assert job["options"]["batch_size"] == 12
    assert job["options"]["api_key_configured"] is True
    assert manager.secrets[job["id"]] == "top-secret"
    raw = (root / job["id"] / "job.json").read_text(encoding="utf-8")
    assert "top-secret" not in raw


def test_round_two_model_falls_back_to_round_one_when_omitted(api_client):
    client, _, _ = api_client
    created = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    ).json()

    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "budget-model", "api_key": "secret",
            "base_url": "https://api.example.com/v1", "dry_run": "false",
        },
    )

    assert response.status_code == 200
    assert response.json()["options"]["round2_model"] == "budget-model"


def test_retry_can_update_endpoint_without_resubmitting_model(api_client):
    client, _, _ = api_client
    job_id = _create_failed_formal_job(client)

    response = client.post(
        f"/api/jobs/{job_id}/retry",
        data={
            "api_key": "replacement-key",
            "base_url": "https://second.example/v1",
        },
    )

    assert response.status_code == 200
    assert response.json()["options"]["model"] == "original-model"
    assert response.json()["options"]["base_url"] == "https://second.example/v1"


def test_retry_model_only_preserves_existing_endpoint(api_client):
    client, _, _ = api_client
    job_id = _create_failed_formal_job(client)

    response = client.post(
        f"/api/jobs/{job_id}/retry",
        data={"api_key": "replacement-key", "model": "replacement-model"},
    )

    assert response.status_code == 200
    assert response.json()["options"]["model"] == "replacement-model"
    assert response.json()["options"]["base_url"] == "https://first.example/v1"


def test_start_translation_persists_proxy(api_client):
    client, _, _ = api_client
    created = _create_ready_job(client)
    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "m", "api_key": "secret",
            "base_url": "https://api.example.com/v1",
            "proxy": "http://127.0.0.1:7890", "dry_run": "false",
        },
    )
    assert response.status_code == 200
    assert response.json()["options"]["proxy"] == "http://127.0.0.1:7890"


def test_start_translation_reuses_explicitly_cleared_saved_proxy(api_client):
    client, _, _ = api_client
    saved = client.put("/api/user-settings", json={"proxy": ""})
    assert saved.status_code == 200
    created = _create_ready_job(client)
    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "m", "api_key": "secret",
            "base_url": "https://api.example.com/v1",
            "proxy": "", "dry_run": "false",
        },
    )
    assert response.status_code == 200
    assert response.json()["options"]["proxy"] == ""


def test_start_translation_rejects_proxy_with_credentials(api_client):
    client, _, _ = api_client
    created = _create_ready_job(client)
    response = client.post(
        f"/api/jobs/{created['id']}/start",
        data={
            "model": "m", "api_key": "secret",
            "base_url": "https://api.example.com/v1",
            "proxy": "http://user:pass@127.0.0.1:7890", "dry_run": "false",
        },
    )
    assert response.status_code == 422


def test_retry_can_update_proxy(api_client):
    client, _, _ = api_client
    job_id = _create_failed_formal_job(client)
    response = client.post(
        f"/api/jobs/{job_id}/retry",
        data={"api_key": "replacement-key", "proxy": "http://127.0.0.1:7890"},
    )
    assert response.status_code == 200
    assert response.json()["options"]["proxy"] == "http://127.0.0.1:7890"


def test_retry_without_proxy_preserves_existing(api_client):
    client, _, _ = api_client
    job_id = _create_failed_formal_job(client)
    first = client.post(
        f"/api/jobs/{job_id}/retry",
        data={"api_key": "k", "proxy": "http://127.0.0.1:7890"},
    )
    assert first.json()["options"]["proxy"] == "http://127.0.0.1:7890"
    assert client.post(f"/api/jobs/{job_id}/force-reset").status_code == 200
    second = client.post(
        f"/api/jobs/{job_id}/retry",
        data={"api_key": "k"},
    )
    assert second.status_code == 200
    assert second.json()["options"]["proxy"] == "http://127.0.0.1:7890"



def test_pause_and_resume_endpoints_work_before_translation(api_client):
    client, _, _ = api_client
    created = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    ).json()

    paused = client.post(f"/api/jobs/{created['id']}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = client.post(f"/api/jobs/{created['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "ready_for_translation"


def test_url_creation_reuses_explicitly_cleared_saved_proxy(api_client):
    client, manager, _ = api_client
    saved = client.put("/api/user-settings", json={"proxy": ""})
    assert saved.status_code == 200
    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "proxy": "", "download_video": "true",
            "video_quality": "1440",
            "download_subtitles": "true", "download_thumbnail": "false",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "queued"
    assert job["options"]["model"] == ""
    assert job["options"]["download_video"] is True
    assert job["options"]["video_quality"] == "1440"
    assert job["options"]["download_thumbnail"] is False
    assert job["options"]["reference_strategy"] == "adaptive"
    assert job["options"]["proxy"] == ""
    assert job["id"] not in manager.secrets


def test_release_defaults_to_fixed_proxy_and_stops_only_its_launcher():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    project_root = WEB_ROOT.parent
    start_script = (project_root / "start_web.bat").read_text(encoding="utf-8")
    stop_script = (project_root / "stop_web.bat").read_text(encoding="utf-8")

    assert 'value="http://127.0.0.1:7890"' in page
    assert 'proxy: "http://127.0.0.1:7890"' in app_script
    assert '--project-root "%~dp0."' in start_script
    assert "web.launcher" in stop_script
    assert "SUBTITLE_PROJECT_MARKER" in stop_script
    assert "netstat" not in stop_script.casefold()


def test_url_creation_rejects_cookie_file_upload_before_persisting(api_client):
    client, _, root = api_client
    cookie_contents = (
        b"# Netscape HTTP Cookie File\n"
        b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-session\n"
    )

    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "download_video": "true",
            "download_subtitles": "true",
            "download_thumbnail": "false",
            "youtube_auth": "file",
        },
        files={
            "youtube_cookies": (
                "cookies.txt",
                cookie_contents,
                "text/plain",
            ),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "请粘贴 cookies.txt 内容"
    assert not list(root.iterdir())


def test_url_creation_keeps_pasted_youtube_cookie_only_in_memory(api_client):
    client, manager, root = api_client
    cookie_text = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tpasted-create-session\n"
    )

    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "download_video": "true",
            "download_subtitles": "true",
            "youtube_auth": "file",
            "youtube_cookies_text": cookie_text,
        },
    )

    assert response.status_code == 200
    job = response.json()
    workspace = root / job["id"]
    assert not (workspace / "youtube.cookies.txt").exists()
    assert manager.private_cookie_store.has_pending(job["id"], 0)
    assert "pasted-create-session" not in (workspace / "job.json").read_text(
        encoding="utf-8"
    )


def test_download_retry_rejects_cookie_file_upload(api_client):
    client, manager, root = api_client
    created = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "download_video": "true",
            "download_subtitles": "true",
            "youtube_auth": "none",
        },
    ).json()
    failed = manager._load(created["id"])
    failed.update(
        status="failed",
        stage="Download failed",
        error="Sign in to confirm you’re not a bot",
        error_code="youtube_auth_required",
    )
    manager._save(failed)
    cookie_contents = (
        b"# Netscape HTTP Cookie File\n"
        b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\treplacement-session\n"
    )

    response = client.post(
        f"/api/jobs/{created['id']}/retry",
        data={"restart": "false", "youtube_auth": "file"},
        files={
            "youtube_cookies": (
                "cookies.txt",
                cookie_contents,
                "text/plain",
            ),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "请粘贴 cookies.txt 内容"
    assert not (root / created["id"] / "youtube.cookies.txt").exists()


def test_download_retry_accepts_pasted_youtube_cookie_text(api_client):
    client, manager, root = api_client
    created = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "download_video": "true",
            "download_subtitles": "true",
        },
    ).json()
    failed = manager._load(created["id"])
    failed.update(
        status="failed",
        stage="Download failed",
        error="HTTP Error 429: Too Many Requests",
        error_code="youtube_auth_required",
    )
    manager._save(failed)
    cookie_text = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tpasted-session\n"
    )

    response = client.post(
        f"/api/jobs/{created['id']}/retry",
        data={
            "restart": "false",
            "youtube_auth": "file",
            "youtube_cookies_text": cookie_text,
        },
    )

    assert response.status_code == 200
    retried = response.json()
    assert retried["options"]["youtube_auth"] == "file"
    generation = retried["generation"]
    assert manager.private_cookie_store.has_pending(created["id"], generation)
    assert not (root / created["id"] / "youtube.cookies.txt").exists()
    assert "pasted-session" not in (root / created["id"] / "job.json").read_text(
        encoding="utf-8"
    )


def test_adaptive_url_source_requires_video_download(api_client):
    client, _, root = api_client
    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "download_video": "false",
            "download_subtitles": "true",
            "reference_strategy": "adaptive",
        },
    )

    assert response.status_code == 422
    assert not list(root.iterdir())


def test_api_rejects_lookalike_youtube_host(api_client):
    client, _, root = api_client
    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://youtube.com.evil.example/watch?v=abcdefghijk",
        },
    )

    assert response.status_code == 422
    assert not list(root.iterdir())


def test_artifact_endpoint_rejects_path_outside_workspace(api_client, tmp_path):
    client, manager, root = api_client
    job_id = "job123"
    workspace = root / job_id
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    job = {
        "id": job_id, "created_at": jobs_module.now(), "updated_at": jobs_module.now(),
        "status": "completed", "stage": "Completed", "generation": 0,
        "workspace": str(workspace), "inputs": {}, "options": {},
        "artifacts": {"outside.txt": str(outside)}, "logs": [], "error": None,
    }
    jobs_module.atomic_json(workspace / "job.json", job)

    response = client.get(f"/api/jobs/{job_id}/artifacts/outside.txt")

    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_prompt_endpoint_rejects_non_string_content(api_client):
    client, _, _ = api_client
    response = client.put("/api/prompt-template", json={"content": {"bad": True}})
    assert response.status_code == 422


def test_english_prompt_api_edits_the_runtime_language_template(
        api_client, tmp_path, monkeypatch):
    client, _, _ = api_client
    project_root = tmp_path / "project"
    data_dir = project_root / "data"
    prompts_dir = data_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    legacy_path = data_dir / "prompt_template.txt"
    legacy_path.write_text(
        "LEGACY\n{glossary}\n{established_terms_section}\n{tricky_terms}\n"
        "{subtitles}\n{format_example}",
        encoding="utf-8",
    )
    runtime_path = prompts_dir / "en-zh-CN.txt"
    runtime_path.write_text(
        "OLD_RUNTIME\n{glossary}\n{established_terms_section}\n{tricky_terms}\n"
        "{subtitles}\n{format_example}",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_module, "PROMPT_TEMPLATE", legacy_path)
    content = (
        "WEB_EDIT_REACHES_RUNTIME\n{glossary}\n{established_terms_section}\n"
        "{tricky_terms}\n{subtitles}\n{format_example}"
    )

    saved = client.put("/api/prompt-template", json={
        "source_language": "en",
        "target_language": "zh-CN",
        "content": content,
    })
    loaded = client.get(
        "/api/prompt-template?source_language=en&target_language=zh-CN"
    )
    runtime_prompt = load_prompt_builder(
        data_dir=str(data_dir), source_language="en", target_language="zh-CN"
    ).build([Subtitle(1, "", "", "Hello")], {})

    assert saved.status_code == 200
    assert loaded.json()["content"] == content
    assert runtime_path.read_text(encoding="utf-8") == content
    assert "WEB_EDIT_REACHES_RUNTIME" in runtime_prompt
    assert "LEGACY" in legacy_path.read_text(encoding="utf-8")


def test_batch_delete_deletes_completed_and_blocks_running(api_client):
    client, manager, root = api_client

    def make_job():
        response = client.post(
            "/api/jobs",
            files={
                "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
                "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
            },
        )
        assert response.status_code == 200
        return response.json()["id"]

    done_id = make_job()
    # Simulate a running job in the manager store
    running_id = make_job()
    job = manager._load(running_id)
    job["status"] = "running"
    manager._save(job)

    response = client.post("/api/jobs/batch-delete", json={"ids": [done_id, running_id, "no-such-id"]})
    assert response.status_code == 200
    payload = response.json()
    assert done_id in payload["deleted"]
    assert running_id in payload["blocked"]
    assert "no-such-id" in payload["not_found"]
    # Deleted workspace is gone; running job still present
    assert not manager._workspace(done_id).exists()
    assert manager._load(running_id)["status"] == "running"


def test_batch_archive_updates_archived_flag(api_client):
    client, _, _ = api_client
    response = client.post(
        "/api/jobs",
        files={
            "capcut_en": ("capcut.srt", SRT, "application/x-subrip"),
            "whisper_en": ("whisper.srt", SRT, "application/x-subrip"),
        },
    )
    job_id = response.json()["id"]

    response = client.post("/api/jobs/batch-archive", json={"ids": [job_id], "archived": True})
    assert response.status_code == 200
    assert response.json()["updated"] == [job_id]
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["archived"] is True

    response = client.post("/api/jobs/batch-archive", json={"ids": [job_id], "archived": False})
    assert response.status_code == 200
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["archived"] is False


def test_batch_retry_accepts_only_homogeneous_failed_jobs(api_client, monkeypatch):
    client, manager, _ = api_client
    first = _create_failed_formal_job(client)
    second = _create_failed_formal_job(client)
    calls = []

    def retry(job_id, secret, restart=False, **kwargs):
        calls.append((job_id, secret, restart))
        return manager.get(job_id)

    monkeypatch.setattr(manager, "retry_failed", retry)

    response = client.post("/api/jobs/batch-retry", json={
        "ids": [first, second], "api_key": "one-use-key", "restart": False,
    })

    assert response.status_code == 200
    assert response.json()["queued"] == [first, second]
    assert calls == [
        (first, "one-use-key", False),
        (second, "one-use-key", False),
    ]
    assert "one-use-key" not in response.text


def test_batch_retry_rejects_mixed_configurations_before_mutation(
        api_client, monkeypatch):
    client, manager, _ = api_client
    first = _create_failed_formal_job(client, base_url="https://first.example/v1")
    second = _create_failed_formal_job(client, base_url="https://second.example/v1")
    calls = []
    monkeypatch.setattr(
        manager, "retry_failed", lambda *args, **kwargs: calls.append(args),
    )

    response = client.post("/api/jobs/batch-retry", json={
        "ids": [first, second], "api_key": "one-use-key",
    })

    assert response.status_code == 422
    assert calls == []
    assert "one-use-key" not in response.text


@pytest.mark.parametrize(("endpoint", "method_name"), [
    ("/api/jobs/batch-delete", "delete"),
    ("/api/jobs/batch-archive", "update_metadata"),
])
def test_batch_operation_errors_are_redacted_and_bounded(
        api_client, monkeypatch, endpoint, method_name):
    client, manager, _ = api_client
    malicious = (
        "Authorization: Bearer sk-live-http Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )

    def reject(*args, **kwargs):
        raise ValueError(malicious)

    monkeypatch.setattr(manager, method_name, reject)
    response = client.post(
        endpoint, json={"ids": ["job123"], "archived": True},
    )

    assert response.status_code == 200
    encoded = response.text
    for secret in (
        "sk-live-http",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
    ):
        assert secret not in encoded
    assert all(
        len(message) <= 240
        for message in response.json()["errors"].values()
    )


def test_batch_delete_requires_id_list(api_client):
    client, _, _ = api_client
    assert client.post("/api/jobs/batch-delete", json={}).status_code == 422
    assert client.post("/api/jobs/batch-delete", json={"ids": []}).status_code == 422


def test_user_settings_api_persists_non_secrets_without_returning_key(api_client):
    client, _, root = api_client

    initial = client.get("/api/user-settings")
    assert initial.status_code == 200
    assert initial.json()["proxy"] == "http://127.0.0.1:7890"
    assert initial.json()["model"] == ""
    assert initial.json()["base_url"] == ""
    assert initial.json()["api_key_configured"] is False

    response = client.put(
        "/api/user-settings",
        json={
            "proxy": "http://127.0.0.1:9000",
            "base_url": "https://api.example.test/v1",
            "model": "chosen-model",
            "round2_model": "",
            "batch_size": 16,
            "local_whisper": False,
            "api_key": "saved-api-secret",
            "workflow_mode": "quality",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key_configured"] is True
    assert payload["api_key_persistence"] == "secure"
    assert "api_key" not in payload
    assert "saved-api-secret" not in response.text
    raw = (root.parent / "settings.json").read_text(encoding="utf-8")
    assert "saved-api-secret" not in raw
    assert "workflow_mode" not in raw


def test_user_settings_api_deletes_saved_api_key(api_client):
    client, _, _ = api_client
    saved = client.put(
        "/api/user-settings", json={"api_key": "saved-api-secret"}
    )
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"] is True

    deleted = client.delete("/api/user-settings/api-key")

    assert deleted.status_code == 200
    assert deleted.json()["api_key_configured"] is False
    assert "saved-api-secret" not in deleted.text


def test_url_creation_saves_and_snapshots_api_configuration(api_client):
    client, manager, root = api_client

    response = client.post(
        "/api/jobs/from-url",
        data={
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "proxy": "http://127.0.0.1:7890",
            "base_url": "https://api.example.test/v1",
            "model": "chosen-model",
            "round2_model": "",
            "api_key": "saved-api-secret",
            "batch_size": "18",
            "local_whisper": "false",
            "download_video": "true",
            "download_subtitles": "true",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["options"]["proxy"] == "http://127.0.0.1:7890"
    assert job["options"]["base_url"] == "https://api.example.test/v1"
    assert job["options"]["model"] == "chosen-model"
    assert job["options"]["round2_model"] == ""
    assert job["options"]["batch_size"] == 18
    assert job["options"]["local_whisper"] is False
    assert job["options"]["api_key_configured"] is True
    assert job["id"] not in manager.secrets
    raw_job = (root / job["id"] / "job.json").read_text(encoding="utf-8")
    assert "saved-api-secret" not in raw_job
    assert '"api_key_configured": true' in raw_job
    public_settings = client.get("/api/user-settings").json()
    assert public_settings["model"] == "chosen-model"
    assert public_settings["api_key_configured"] is True
    assert "saved-api-secret" not in json.dumps(public_settings)


def test_uploaded_srt_job_inherits_saved_non_secret_configuration(api_client):
    client, _, _ = api_client
    saved = client.put(
        "/api/user-settings",
        json={
            "proxy": "http://127.0.0.1:7890",
            "base_url": "https://api.example.test/v1",
            "model": "chosen-model",
            "round2_model": "review-model",
            "batch_size": 15,
            "local_whisper": False,
            "api_key": "saved-api-secret",
        },
    )
    assert saved.status_code == 200

    created = _create_ready_job(client)

    assert created["options"]["model"] == "chosen-model"
    assert created["options"]["base_url"] == "https://api.example.test/v1"
    assert created["options"]["proxy"] == "http://127.0.0.1:7890"
    assert created["options"]["batch_size"] == 15
    assert created["options"]["api_key_configured"] is True


def test_start_and_retry_reuse_secure_api_key_without_resubmission(api_client):
    client, manager, _ = api_client
    saved = client.put(
        "/api/user-settings",
        json={
            "base_url": "https://api.example.test/v1",
            "model": "chosen-model",
            "api_key": "saved-api-secret",
        },
    )
    assert saved.status_code == 200
    created = _create_ready_job(client)

    started = client.post(f"/api/jobs/{created['id']}/start", data={})

    assert started.status_code == 200
    assert started.json()["options"]["model"] == "chosen-model"
    assert manager.secrets[created["id"]] == "saved-api-secret"

    reset = client.post(f"/api/jobs/{created['id']}/force-reset")
    assert reset.status_code == 200
    assert created["id"] not in manager.secrets
    retried = client.post(
        f"/api/jobs/{created['id']}/retry",
        data={"model": "replacement-model",
              "base_url": "https://replacement.example/v1"},
    )
    assert retried.status_code == 200
    assert manager.secrets[created["id"]] == "saved-api-secret"
    reusable = client.get("/api/user-settings").json()
    assert reusable["model"] == "replacement-model"
    assert reusable["base_url"] == "https://replacement.example/v1"


def test_batch_retry_reuses_secure_api_key_without_resubmission(
        api_client, monkeypatch):
    client, manager, _ = api_client
    first = _create_failed_formal_job(client)
    second = _create_failed_formal_job(client)
    saved = client.put(
        "/api/user-settings", json={"api_key": "saved-api-secret"},
    )
    assert saved.status_code == 200
    calls = []

    def retry(job_id, secret, restart=False, **kwargs):
        calls.append((job_id, secret, restart))
        return manager.get(job_id)

    monkeypatch.setattr(manager, "retry_failed", retry)
    response = client.post(
        "/api/jobs/batch-retry", json={"ids": [first, second]},
    )

    assert response.status_code == 200
    assert calls == [
        (first, "saved-api-secret", False),
        (second, "saved-api-secret", False),
    ]
    assert "saved-api-secret" not in response.text


def test_legacy_waiting_job_can_continue_with_automatic_source(api_client, monkeypatch):
    client, manager, _ = api_client
    calls = []

    def continue_legacy(job_id):
        calls.append(job_id)
        return {"id": job_id, "status": "ready_for_translation"}

    monkeypatch.setattr(manager, "continue_with_automatic_source", continue_legacy)
    response = client.post("/api/jobs/legacy-waiting/continue-with-automatic-source")

    assert response.status_code == 200
    assert response.json()["status"] == "ready_for_translation"
    assert calls == ["legacy-waiting"]


def test_landing_page_describes_the_linear_flow_without_modes_or_model_advice():
    landing = (WEB_ROOT / "landing" / "index.html").read_text(encoding="utf-8")

    assert "填写必要的 API 配置" in landing
    assert "下载并翻译" in landing
    assert "快速模式" not in landing
    assert "高质量模式" not in landing
    assert "便宜模型" not in landing
