from __future__ import annotations

import sys
import os

# Windows UTF-8: reconfigure stdio before any logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import importlib.util
import importlib
import json
import logging
import re
import shutil
import subprocess
import string
import tempfile
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.params import Param
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pipeline.config import normalize_llm_base_url
from pipeline.languages import normalize_language_pair
from pipeline.safe_errors import public_error_fields, safe_public_text
from pipeline import subtitle_burner
from web.jobs import JOBS_ROOT, STUCK_JOB_CHECK_INTERVAL_SECONDS, manager
from web.user_settings import (
    CredentialStoreUnavailable,
    UserSettingsStore,
)
from web.local_access import (
    BOOTSTRAP_HEADER_NAME,
    CSRF_HEADER_NAME,
    LAUNCH_CHALLENGE_HEADER_NAME,
    SESSION_COOKIE_NAME,
    local_access,
)

WEB_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_ROOT.parent
PROMPT_TEMPLATE = PROJECT_ROOT / "data" / "prompts" / "en-zh-CN.txt"
PROMPT_LOCK = threading.Lock()
PROJECT_VERSION = (PROJECT_ROOT / "VERSION").read_text(
    encoding="utf-8"
).strip()
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_REQUEST_BODY_BYTES = 110 * 1024 * 1024
DEFAULT_MAX_SSE_CONNECTIONS = 8
MAX_YOUTUBE_COOKIE_BYTES = 1024 * 1024
MAX_PROMPT_CHARS = 1_000_000
MEDIA_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg",
}
CAPCUT_EXECUTABLE_NAMES = {"jianyingpro.exe", "capcut.exe"}
user_settings = UserSettingsStore()


def _safe_validation_detail(
    error: BaseException,
    fallback: str = "请求参数或任务状态无效",
) -> str:
    return safe_public_text(str(error), fallback=fallback)


def _public_payload(payload):
    """Keep local task file references relative at every JSON API boundary."""
    return manager.public_payload(payload)


def _language_pair_or_422(
    source_language: str | None = None,
    target_language: str | None = None,
) -> dict[str, str]:
    try:
        values = {}
        if source_language is not None:
            values["source_language"] = source_language
        if target_language is not None:
            values["target_language"] = target_language
        return normalize_language_pair(values)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, _safe_validation_detail(error))


def _prompt_template_for(pair: dict[str, str]) -> Path:
    """Return the language-pair template used by the runtime loader."""
    return (
        PROJECT_ROOT / "data" / "prompts"
        / f"{pair['source_language']}-{pair['target_language']}.txt"
    )


def _registry_display_icon_path(value: str) -> str:
    """Extract an executable path from a Windows DisplayIcon value."""
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith('"'):
        closing = value.find('"', 1)
        return value[1:closing] if closing > 1 else ""
    return value.rsplit(",", 1)[0].strip()


def _windows_capcut_registry_candidates() -> list[Path]:
    """Return per-user and machine-wide CapCut/Jianying registry installs."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    candidates: list[Path] = []
    uninstall_keys = (
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, key_path in uninstall_keys:
        try:
            root = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    subkey = winreg.OpenKey(root, subkey_name)
                except OSError:
                    continue
                with subkey:
                    try:
                        display_name = str(
                            winreg.QueryValueEx(subkey, "DisplayName")[0]
                        )
                    except OSError:
                        display_name = ""
                    if not re.search(r"剪映|jianying|capcut", display_name, re.I):
                        continue
                    try:
                        install_location = str(
                            winreg.QueryValueEx(subkey, "InstallLocation")[0]
                        ).strip()
                    except OSError:
                        install_location = ""
                    if install_location:
                        install_root = Path(install_location)
                        for executable_name in CAPCUT_EXECUTABLE_NAMES:
                            candidates.extend((
                                install_root / executable_name,
                                install_root / "Apps" / executable_name,
                            ))
                    try:
                        display_icon = _registry_display_icon_path(
                            winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                        )
                    except OSError:
                        display_icon = ""
                    if display_icon:
                        candidates.append(Path(display_icon))
    return candidates


def _capcut_executable_candidates() -> list[Path]:
    """Build portable install candidates without embedding a Windows username."""
    candidates: list[Path] = []
    override = os.getenv("SUBTITLE_CAPCUT_EXECUTABLE", "").strip()
    if override:
        candidates.append(Path(override).expanduser())

    for executable_name in CAPCUT_EXECUTABLE_NAMES:
        resolved = shutil.which(executable_name)
        if resolved:
            candidates.append(Path(resolved))

    local_value = os.getenv("LOCALAPPDATA", "").strip()
    profile_value = os.getenv("USERPROFILE", "").strip()
    program_values = [
        os.getenv("ProgramFiles", "").strip(),
        os.getenv("ProgramFiles(x86)", "").strip(),
    ]
    if local_value:
        local = Path(local_value)
        candidates.extend((
            local / "JianyingPro" / "Apps" / "JianyingPro.exe",
            local / "CapCut" / "Apps" / "CapCut.exe",
            local / "Programs" / "CapCut" / "CapCut.exe",
        ))
        for app_root, executable_name in (
            (local / "JianyingPro" / "Apps", "JianyingPro.exe"),
            (local / "CapCut" / "Apps", "CapCut.exe"),
        ):
            if app_root.is_dir():
                candidates.extend(app_root.glob(f"*/{executable_name}"))
    if profile_value:
        candidates.append(
            Path(profile_value) / "Desktop" / "CapCut" / "CapCut.exe"
        )
    for root_value in program_values:
        if root_value:
            root = Path(root_value)
            candidates.extend((
                root / "JianyingPro" / "JianyingPro.exe",
                root / "CapCut" / "CapCut.exe",
            ))
    candidates.extend(_windows_capcut_registry_candidates())
    return candidates


def _find_capcut_executable() -> Path | None:
    """Return the first real Jianying/CapCut executable from trusted sources."""
    seen: set[str] = set()
    for candidate in _capcut_executable_candidates():
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if (resolved.is_file()
                and resolved.name.casefold() in CAPCUT_EXECUTABLE_NAMES):
            return resolved
    return None


def _upload_limit() -> int:
    raw = os.getenv("SUBTITLE_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        value = int(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid SUBTITLE_MAX_UPLOAD_BYTES=%r; using default", raw
        )
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


MAX_UPLOAD_BYTES = _upload_limit()


def _request_body_limit() -> int:
    raw = os.getenv(
        "SUBTITLE_MAX_REQUEST_BODY_BYTES",
        str(DEFAULT_MAX_REQUEST_BODY_BYTES),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Invalid SUBTITLE_MAX_REQUEST_BODY_BYTES; using safe default"
        )
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_REQUEST_BODY_BYTES


MAX_REQUEST_BODY_BYTES = _request_body_limit()


def _sse_limit() -> int:
    raw = os.getenv("SUBTITLE_MAX_SSE_CONNECTIONS", str(DEFAULT_MAX_SSE_CONNECTIONS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SSE_CONNECTIONS
    return value if value > 0 else DEFAULT_MAX_SSE_CONNECTIONS


MAX_SSE_CONNECTIONS = _sse_limit()
_sse_lock = threading.Lock()
_sse_connections = 0


def _acquire_sse_slot() -> bool:
    global _sse_connections
    with _sse_lock:
        if _sse_connections >= MAX_SSE_CONNECTIONS:
            return False
        _sse_connections += 1
        return True


def _release_sse_slot() -> None:
    global _sse_connections
    with _sse_lock:
        _sse_connections = max(0, _sse_connections - 1)


@lru_cache(maxsize=None)
def _module_imports(name: str) -> bool:
    """Check that an optional capability actually imports, not just exists."""
    try:
        importlib.import_module(name)
    except (ImportError, OSError):
        return False
    return True


def _form_value(value):
    if isinstance(value, Param) or (
        not isinstance(value, (str, int, float, bool, type(None)))
        and hasattr(value, "default")
    ):
        return value.default
    return value


def _clean_text(value, label: str, *, required: bool = True, limit: int = 200) -> str:
    value = _form_value(value)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HTTPException(422, f"{label} 必须是文本")
    value = value.strip()
    if required and not value:
        raise HTTPException(422, f"{label}不能为空")
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise HTTPException(422, f"{label}格式无效")
    return value


def _validate_batch_size(value: int) -> int:
    value = int(_form_value(value))
    if not 1 <= value <= 100:
        raise HTTPException(422, "批次大小必须在 1 到 100 之间")
    return value


def _validate_base_url(value: str | None, *, required: bool = False) -> str:
    value = _clean_text(value, "API 接口地址", required=False, limit=2048)
    try:
        return normalize_llm_base_url(value, required=required)
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error)) from None


def _validate_proxy(value: str) -> str:
    value = _clean_text(value, "代理地址", required=False, limit=2048)
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname:
        raise HTTPException(422, "代理地址格式无效")
    if parsed.username or parsed.password:
        raise HTTPException(
            422,
            "代理地址不能包含用户名或密码；请使用无凭据的本地代理地址",
        )
    return value


USER_TRANSLATION_SETTING_FIELDS = (
    "proxy", "base_url", "model", "round2_model", "batch_size",
    "local_whisper",
)


def _validate_user_settings_update(payload: dict) -> dict:
    validated = {}
    if "proxy" in payload:
        validated["proxy"] = _validate_proxy(payload["proxy"])
    if "base_url" in payload:
        validated["base_url"] = _validate_base_url(
            payload["base_url"], required=False,
        )
    for field, label in (
        ("model", "模型名称"),
        ("round2_model", "Round 2 模型名称"),
    ):
        if field in payload:
            validated[field] = _clean_text(
                payload[field], label, required=False, limit=200,
            )
    if "batch_size" in payload:
        validated["batch_size"] = _validate_batch_size(payload["batch_size"])
    if "local_whisper" in payload:
        if not isinstance(payload["local_whisper"], bool):
            raise HTTPException(422, "局部 Whisper 设置必须是布尔值")
        validated["local_whisper"] = payload["local_whisper"]
    if "api_key" in payload:
        validated["api_key"] = _clean_text(
            payload["api_key"], "API key", required=False, limit=8192,
        )
    if "api_key_storage" in payload:
        storage = payload["api_key_storage"]
        if storage not in {"session", "secure"}:
            raise HTTPException(422, "API Key 保存方式无效")
        validated["api_key_storage"] = storage
    return validated


def _persist_user_settings(payload: dict) -> dict:
    try:
        return user_settings.update(_validate_user_settings_update(payload))
    except (OSError, ValueError):
        raise HTTPException(500, "用户设置保存失败") from None


def _translation_settings_snapshot(public: dict | None = None) -> dict:
    current = public or user_settings.get_public_settings()
    return {
        **{field: current[field] for field in USER_TRANSLATION_SETTING_FIELDS},
        "api_key_configured": bool(current.get("api_key_configured")),
    }


def _resolve_request_api_key(
    value: str | None, storage: str = "secure",
) -> str:
    secret = _clean_text(value, "API key", required=False, limit=8192)
    if secret:
        _persist_user_settings({"api_key": secret, "api_key_storage": storage})
    return user_settings.resolve_api_key(secret)


def _validate_language_pair(source_language, target_language) -> dict[str, str]:
    try:
        return normalize_language_pair({
            "source_language": _form_value(source_language),
            "target_language": _form_value(target_language),
        })
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error)) from error


def _run_stuck_job_watchdog(stop: threading.Event) -> None:
    """Periodically reset jobs whose worker died or stopped making progress."""
    while not stop.wait(STUCK_JOB_CHECK_INTERVAL_SECONDS):
        try:
            manager.reset_stuck_jobs()
        except Exception:
            # 看门狗自身出错绝不能拖垮服务；下一轮扫描会再试。
            logging.getLogger(__name__).warning(
                "Stuck-job watchdog iteration failed", exc_info=True,
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manager.initialize_secure_runtime()
    manager.recover_interrupted_jobs()
    watchdog_stop = threading.Event()
    watchdog = threading.Thread(
        target=_run_stuck_job_watchdog,
        args=(watchdog_stop,),
        name="subtitle-stuck-job-watchdog",
        daemon=True,
    )
    watchdog.start()
    try:
        yield
    finally:
        watchdog_stop.set()
        watchdog.join(timeout=5)
        manager.close()


app = FastAPI(
    title="Wuwa Subtitle Console",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/launcher-ready",
    "/api/session/bootstrap",
}
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def configure_local_access(
    *,
    session_token: str,
    bootstrap_token: str,
    csrf_token: str,
    launch_challenge: str,
) -> None:
    """Install launcher-generated secrets without environment or disk storage."""
    local_access.configure(
        session_token=session_token,
        bootstrap_token=bootstrap_token,
        csrf_token=csrf_token,
        launch_challenge=launch_challenge,
    )


def _same_origin(origin: str, request: Request) -> bool:
    """Accept only a syntactically valid origin matching the validated Host."""
    try:
        parsed = urlparse(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return False
    request_host = request.url.hostname
    if not request_host or parsed.hostname.casefold() != request_host.casefold():
        return False
    request_port = request.url.port
    if origin_port is None:
        origin_port = 443 if parsed.scheme == "https" else 80
    if request_port is None:
        request_port = 443 if request.url.scheme == "https" else 80
    return parsed.scheme == request.url.scheme and origin_port == request_port


def _protect_response(response):
    """Attach browser privacy headers to normal and rejected responses."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    return response


@app.middleware("http")
async def protect_local_browser_boundary(request: Request, call_next):
    """Authenticate local APIs and block cross-site browser requests."""
    path = request.url.path
    if path.startswith("/api/"):
        fetch_site = request.headers.get("sec-fetch-site", "").casefold()
        if fetch_site == "cross-site":
            return _protect_response(
                JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            )
        if path not in PUBLIC_API_PATHS:
            session = request.cookies.get(SESSION_COOKIE_NAME, "")
            if not local_access.valid_session(session):
                return _protect_response(
                    JSONResponse(
                        {"detail": "工作台会话无效，请关闭页面后重新启动", "code": "local_session_invalid"},
                        status_code=401,
                    )
                )
            if (
                request.method in STATE_CHANGING_METHODS
                and not local_access.valid_csrf(
                    request.headers.get(CSRF_HEADER_NAME, "")
                )
            ):
                return _protect_response(
                    JSONResponse(
                        {"detail": "工作台安全校验失败，请刷新页面", "code": "local_csrf_invalid"},
                        status_code=403,
                    )
                )
    if request.method in STATE_CHANGING_METHODS:
        origin = request.headers.get("origin")
        if origin and not _same_origin(origin, request):
            return _protect_response(
                JSONResponse({"detail": "拒绝跨来源操作"}, status_code=403)
            )
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return _protect_response(response)


class _RequestBodyTooLarge(Exception):
    """Internal control flow for rejecting chunked bodies before route parsing."""


@app.middleware("http")
async def enforce_request_body_limit(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > MAX_REQUEST_BODY_BYTES:
            return _protect_response(
                JSONResponse(
                    {"detail": "请求体超过本机安全上限"}, status_code=413
                )
            )
    except (TypeError, ValueError):
        # An invalid length is handled by the streaming counter below.
        pass

    original_receive = request._receive
    received = 0

    async def limited_receive():
        nonlocal received
        message = await original_receive()
        if message.get("type") == "http.request":
            received += len(message.get("body") or b"")
            if received > MAX_REQUEST_BODY_BYTES:
                raise _RequestBodyTooLarge
        return message

    request._receive = limited_receive
    try:
        return await call_next(request)
    except _RequestBodyTooLarge:
        return _protect_response(
            JSONResponse(
                {"detail": "请求体超过本机安全上限"}, status_code=413
            )
        )


app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")


# ============================================================
# Glossary API
# ============================================================

def _glossary_db():
    from pipeline.preprocess.glossary import load_glossary_db
    return load_glossary_db()


@app.get("/api/glossary")
def glossary_list(game: str = "wuwa", category: str = None, q: str = None,
                  source_language: str = "en", target_language: str = "zh-CN"):
    pair = _language_pair_or_422(source_language, target_language)
    db = _glossary_db()
    terms = db.list_all(game=game, category=category, **pair)
    if q:
        ql = q.lower()
        terms = [t for t in terms if ql in t["english"].lower() or ql in t["chinese"].lower()]
    return terms


@app.get("/api/glossary/stats")
def glossary_stats():
    db = _glossary_db()
    return db.stats()


@app.post("/api/glossary")
async def glossary_add(payload: dict):
    pair = _language_pair_or_422(
        payload.get("source_language"), payload.get("target_language"),
    )
    payload = {
        **payload,
        "english": payload.get("source_term", payload.get("english")),
        "chinese": payload.get("target_term", payload.get("chinese")),
    }
    english = _clean_text(payload.get("english"), "英文", limit=200)
    chinese = _clean_text(payload.get("chinese"), "中文", limit=500)
    game = _clean_text(payload.get("game") or "wuwa", "游戏", limit=50)
    category = _clean_text(
        payload.get("category") or "", "分类", required=False, limit=50
    )
    db = _glossary_db()
    db.insert(english, chinese, game=game, category=category, **pair)
    return {"ok": True, "english": english, "chinese": chinese,
            "source_term": english, "target_term": chinese, **pair}


@app.put("/api/glossary/{english}")
async def glossary_update(english: str, payload: dict):
    db = _glossary_db()
    pair = _language_pair_or_422(
        payload.get("source_language"), payload.get("target_language"),
    )
    payload = {
        **payload,
        "english": payload.get("source_term", payload.get("english")),
        "chinese": payload.get("target_term", payload.get("chinese")),
    }
    english = _clean_text(english, "英文", limit=200)
    new_english = _clean_text(
        payload.get("english") or english, "英文", limit=200
    )
    game = _clean_text(payload.get("game") or "wuwa", "游戏", limit=50)
    new_chinese = _clean_text(payload.get("chinese"), "中文", limit=500)
    new_category = _clean_text(
        payload.get("category") or "", "分类", required=False, limit=50
    )
    try:
        updated = db.update(
            english, new_english, new_chinese, game=game, category=new_category,
            **pair,
        )
    except Exception as error:
        if "UNIQUE constraint" in str(error):
            raise HTTPException(409, "目标英文术语已经存在")
        raise
    if not updated:
        raise HTTPException(404, "术语不存在")
    return {"ok": True}


@app.delete("/api/glossary/{english}")
def glossary_delete(english: str, game: str = "wuwa",
                    source_language: str = "en", target_language: str = "zh-CN"):
    pair = _language_pair_or_422(source_language, target_language)
    db = _glossary_db()
    deleted = db.delete(english, game=game, **pair)
    if not deleted:
        raise HTTPException(404, "术语不存在")
    return {"ok": True, "deleted": english}


# ============================================================
# Prompt Template API
# ============================================================

@app.get("/api/prompt-template")
def get_prompt_template(source_language: str = "en", target_language: str = "zh-CN"):
    pair = _language_pair_or_422(source_language, target_language)
    template_path = _prompt_template_for(pair)
    if not template_path.is_file():
        raise HTTPException(404, f"prompt template not found for {pair['source_language']}→{pair['target_language']}")
    return {"content": template_path.read_text(encoding="utf-8"), **pair}


@app.put("/api/prompt-template")
async def update_prompt_template(payload: dict):
    pair = _language_pair_or_422(
        payload.get("source_language"), payload.get("target_language"),
    )
    template_path = _prompt_template_for(pair)
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(422, "缺少 content 字段")
    if len(content) > MAX_PROMPT_CHARS:
        raise HTTPException(413, "提示词模板过大")
    required = {
        "glossary", "subtitles", "format_example",
        "established_terms_section", "tricky_terms",
    }
    allowed = required | {
        "global_context_section", "dialogue_memory_section",
        "translation_memory_section",
    }
    try:
        fields = {
            field_name for _, field_name, _, _ in string.Formatter().parse(content)
            if field_name
        }
    except ValueError as error:
        detail = _safe_validation_detail(error, "提示词模板格式错误")
        raise HTTPException(422, f"提示词模板格式错误: {detail}")
    unknown = fields - allowed
    missing = required - fields
    if unknown:
        raise HTTPException(422, f"未知模板变量: {', '.join(sorted(unknown))}")
    if missing:
        raise HTTPException(422, f"缺少模板变量: {', '.join(sorted(missing))}")
    template_path.parent.mkdir(parents=True, exist_ok=True)
    with PROMPT_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            prefix=template_path.name + ".", suffix=".tmp",
            dir=template_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_name, template_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return {"ok": True, "size": len(content), **pair}


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_ROOT / "landing" / "index.html").read_text(encoding="utf-8")


@app.get("/app", response_class=HTMLResponse)
def workbench():
    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")


@app.post("/api/session/bootstrap")
def bootstrap_local_session(request: Request):
    """Exchange the launcher's one-time fragment secret for a session cookie."""
    exchanged = local_access.exchange_bootstrap(
        request.headers.get(BOOTSTRAP_HEADER_NAME, "")
    )
    if exchanged is None:
        raise HTTPException(401, "工作台启动凭据无效或已使用")
    session_token, csrf_token = exchanged
    response = JSONResponse({"ok": True, "csrf_token": csrf_token})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=False,  # Loopback launcher intentionally serves plain HTTP.
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/session")
def local_session_status(request: Request):
    """Restore the in-memory CSRF value after a same-browser page refresh."""
    csrf_token = local_access.csrf_for_session(
        request.cookies.get(SESSION_COOKIE_NAME, "")
    )
    if csrf_token is None:  # Middleware normally rejects this first.
        raise HTTPException(401, "工作台会话无效")
    return {"ok": True, "csrf_token": csrf_token}


@app.get("/api/launcher-ready", status_code=204)
def launcher_ready(request: Request):
    """Prove the pre-bound listener belongs to this launcher instance."""
    if not local_access.valid_launch_challenge(
        request.headers.get(LAUNCH_CHALLENGE_HEADER_NAME, "")
    ):
        raise HTTPException(404, "Not found")
    return Response(status_code=204)


def _recent_failure_codes(limit: int = 5) -> list[str]:
    """Return the newest failed jobs' public error codes, newest first.

    ``error_code`` is already normalised to the public allowlist when a job is
    persisted, so this exposes stable codes and never upstream raw text.
    """
    codes: list[str] = []
    for job in manager.list():
        if job.get("status") != "failed":
            continue
        codes.append(str(job.get("error_code") or "internal_error"))
        if len(codes) >= limit:
            break
    return codes


def _install_hints(capabilities: dict[str, bool]) -> dict[str, str]:
    """Return self-install guidance for missing optional capabilities.

    工作台绝不自动下载任何二进制（供应链加固立场）；缺失时只给指引文案。
    """
    hints: dict[str, str] = {}
    if not capabilities.get("ffmpeg"):
        hints["ffmpeg"] = (
            "请自行安装 FFmpeg 并加入 PATH 后重启工作台；工作台不会自动"
            "下载任何组件。没有它仍可翻译已有 SRT 与带字幕的 YouTube 视频。"
        )
    return hints


@app.get("/api/health")
def health():
    burn_capability = subtitle_burner.check_burn_capability()
    capabilities = {
        "youtube_download": bool(
            shutil.which("yt-dlp")
            or importlib.util.find_spec("yt_dlp") is not None
        ),
        "local_whisper": _module_imports("faster_whisper"),
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }
    return {
        "ok": True,
        "version": PROJECT_VERSION,
        "distribution": os.getenv("SUBTITLE_DISTRIBUTION", "source"),
        "capabilities": capabilities,
        "subtitle_burn": {
            "available": burn_capability.available,
            "error_code": burn_capability.error_code,
            "message": burn_capability.message,
            "style": subtitle_burner.BLACK_OUTLINE_WHITE_2,
        },
        "stuck_jobs": manager.stuck_job_report(),
        "recent_failures": _recent_failure_codes(),
        "disk": manager.disk_usage_report(),
        "warnings": [
            label
            for key, label in {
                "youtube_download": "YouTube 下载器不可用",
                "local_whisper": "本地 Whisper 未安装",
                "ffmpeg": "FFmpeg 未安装（视频下载不可用）",
            }.items()
            if not capabilities[key]
        ],
        "install_hints": _install_hints(capabilities),
    }


@app.get("/api/jobs")
def list_jobs():
    return _public_payload(manager.list())


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, summary: bool = False):
    try:
        job = manager.get(job_id, detailed=False) if summary else manager.get(job_id)
        return _public_payload(job)
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")


@app.post("/api/jobs/{job_id}/exports/burned-video")
def request_burned_video(job_id: str):
    try:
        job = manager.request_burned_video(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(409, _safe_validation_detail(error))
    export = (job.get("exports") or {}).get("burned_video") or {}
    status_code = 202 if export.get("status") in {"queued", "running"} else 200
    return JSONResponse(
        content=_public_payload(job),
        status_code=status_code,
    )


@app.post("/api/jobs/{job_id}/exports/burned-video/cancel")
def cancel_burned_video(job_id: str):
    try:
        return _public_payload(manager.cancel_burned_video(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(409, _safe_validation_detail(error))


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, payload: dict):
    custom_name = payload.get("custom_name") if "custom_name" in payload else None
    archived = payload.get("archived") if "archived" in payload else None
    if custom_name is not None:
        custom_name = _clean_text(
            custom_name, "任务名称", required=False, limit=200
        )
    if archived is not None and not isinstance(archived, bool):
        raise HTTPException(422, "归档状态必须是布尔值")
    try:
        return _public_payload(manager.update_metadata(
            job_id, custom_name=custom_name, archived=archived
        ))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")


@app.post("/api/jobs/{job_id}/cleanup/{kind}")
def cleanup_job_files(job_id: str, kind: str):
    if kind not in {"video", "technical"}:
        raise HTTPException(422, "无效的清理类型")
    try:
        return _public_payload(manager.cleanup(job_id, kind))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/organize")
def organize_job_files(job_id: str):
    try:
        return _public_payload(manager.organize(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.get("/api/user-settings")
def get_user_settings():
    return user_settings.get_public_settings()


@app.put("/api/user-settings")
def update_user_settings(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(422, "设置必须是对象")
    return _persist_user_settings(payload)


@app.delete("/api/user-settings/api-key")
def delete_user_settings_api_key():
    try:
        return user_settings.delete_api_key()
    except CredentialStoreUnavailable:
        raise HTTPException(
            503, "系统安全存储不可用，无法删除已保存的 API Key"
        ) from None


@app.post("/api/jobs/{job_id}/open-folder")
def open_job_folder(job_id: str):
    try:
        manager.get(job_id)
        workspace = manager._workspace(job_id).resolve()
        workspace.relative_to(manager._workspace(job_id).parent.resolve())
        if os.name == "nt":
            os.startfile(str(workspace))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(workspace)])
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except (OSError, ValueError):
        raise HTTPException(400, "无法打开任务目录")
    return {"ok": True}


@app.post("/api/downloads/open")
def open_downloads_folder():
    try:
        if os.name == "nt":
            os.startfile(str(JOBS_ROOT))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(JOBS_ROOT)])
    except OSError:
        raise HTTPException(500, "无法打开 Download 文件夹")
    return {"ok": True}


@app.post("/api/apps/capcut/open")
def open_capcut_app():
    executable = _find_capcut_executable()
    if executable is None:
        raise HTTPException(
            404,
            "未找到剪映专业版或 CapCut。请先安装应用，或设置 "
            "SUBTITLE_CAPCUT_EXECUTABLE 指向 JianyingPro.exe/CapCut.exe",
        )
    options = {
        "cwd": str(executable.parent),
        "close_fds": True,
    }
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        if creationflags:
            options["creationflags"] = creationflags
    try:
        subprocess.Popen([str(executable)], **options)
    except OSError:
        raise HTTPException(500, "剪映启动失败，请检查应用安装状态")
    return {"ok": True, "app": executable.stem}


async def persist_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,15}", suffix):
        suffix = ".bin"
    fd, path = tempfile.mkstemp(prefix="subtitle-upload-", suffix=suffix)
    os.close(fd)
    target = Path(path)
    total = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"Upload exceeds the {MAX_UPLOAD_BYTES}-byte limit",
                    )
                output.write(chunk)
        if total == 0:
            raise HTTPException(422, "上传文件不能为空")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def parse_youtube_cookie_text(content: str) -> bytearray:
    """Validate Netscape cookies in memory; never write them in a route."""
    encoded = bytearray(str(content or "").encode("utf-8"))
    if not encoded:
        raise HTTPException(422, "请粘贴 cookies.txt 的文本内容")
    if len(encoded) > MAX_YOUTUBE_COOKIE_BYTES:
        encoded.clear()
        raise HTTPException(413, "Cookie 文本不能超过 1 MiB")
    header = bytes(encoded[:4096]).decode("utf-8-sig", errors="replace")
    first_line = header.splitlines()[0].strip() if header.splitlines() else ""
    if first_line not in {
        "# Netscape HTTP Cookie File",
        "# HTTP Cookie File",
    }:
        encoded[:] = b"\0" * len(encoded)
        encoded.clear()
        raise HTTPException(
            422,
            "Cookie 格式不正确；请使用 Netscape 格式的 cookies.txt 内容",
        )
    return encoded


@app.post("/api/jobs/from-url")
async def create_job_from_url(
    url: str = Form(...),
    proxy: Optional[str] = Form(None),
    base_url: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    round2_model: Optional[str] = Form(None),
    api_key: str = Form(""),
    api_key_storage: str = Form("secure"),
    batch_size: Optional[int] = Form(None),
    local_whisper: Optional[bool] = Form(None),
    download_video: bool = Form(True),
    download_subtitles: bool = Form(True),
    download_thumbnail: bool = Form(True),
    video_quality: str = Form("1080"),
    reference_strategy: str = Form("adaptive"),
    youtube_auth: str = Form("none"),
    source_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
    youtube_cookies_text: Optional[str] = Form(None),
):
    download_video = bool(_form_value(download_video))
    download_subtitles = bool(_form_value(download_subtitles))
    download_thumbnail = bool(_form_value(download_thumbnail))
    url = _clean_text(url, "视频链接", limit=2048)
    settings_updates = {}
    for field, value in (
        ("proxy", _form_value(proxy)),
        ("base_url", _form_value(base_url)),
        ("model", _form_value(model)),
        ("round2_model", _form_value(round2_model)),
        ("batch_size", _form_value(batch_size)),
        ("local_whisper", _form_value(local_whisper)),
    ):
        if value is not None:
            settings_updates[field] = value
    supplied_key = _clean_text(api_key, "API key", required=False, limit=8192)
    if supplied_key:
        settings_updates["api_key"] = supplied_key
        settings_updates["api_key_storage"] = api_key_storage
    public_settings = _persist_user_settings(settings_updates)
    translation_snapshot = _translation_settings_snapshot(public_settings)
    proxy_value = translation_snapshot["proxy"]
    reference_strategy = str(reference_strategy).strip().lower()
    video_quality = str(video_quality).strip().lower()
    youtube_auth = str(youtube_auth).strip().lower()
    language_pair = _validate_language_pair(source_language, target_language)
    if reference_strategy not in {"adaptive", "youtube", "whisper"}:
        raise HTTPException(422, "无效的第二路源语言证据策略")
    if video_quality not in {"best", "2160", "1440", "1080", "720", "480"}:
        raise HTTPException(422, "无效的视频清晰度")
    if youtube_auth not in {"none", "chrome", "file"}:
        raise HTTPException(422, "无效的 YouTube 认证方式")
    pasted_cookies = youtube_cookies_text or ""
    has_pasted = bool(pasted_cookies.strip())
    if youtube_auth == "file" and not has_pasted:
        raise HTTPException(422, "请粘贴 cookies.txt 内容")
    if youtube_auth != "file" and has_pasted:
        raise HTTPException(422, "粘贴 Cookie 时认证方式必须选择 cookies.txt")
    if reference_strategy in {"adaptive", "whisper"} and not download_video:
        raise HTTPException(422, "自适应或本地 Whisper 策略需要下载视频")
    from web.downloads import extract_video_id
    try:
        extract_video_id(url)
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))
    if not download_subtitles:
        raise HTTPException(422, "URL 翻译任务必须下载源语言字幕；需要交叉证据时请启用本地 Whisper")
    cookie_payload = None
    try:
        if has_pasted:
            cookie_payload = parse_youtube_cookie_text(pasted_cookies)
        return _public_payload(manager.create_from_url(
            url,
            {
                **translation_snapshot,
                "proxy": proxy_value,
                "download_video": download_video,
                "download_subtitles": download_subtitles,
                "download_thumbnail": download_thumbnail,
                "video_quality": video_quality,
                "reference_strategy": reference_strategy,
                "youtube_auth": youtube_auth,
                **language_pair,
            },
            youtube_cookie=cookie_payload,
        ))
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))
    finally:
        if cookie_payload is not None:
            cookie_payload[:] = b"\0" * len(cookie_payload)
            cookie_payload.clear()


@app.post("/api/jobs/{job_id}/continue-with-automatic-source")
def continue_with_automatic_source(job_id: str):
    try:
        return _public_payload(
            manager.continue_with_automatic_source(job_id)
        )
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/capcut")
async def attach_capcut(
    job_id: str,
    capcut_en: UploadFile = File(...),
    source_language: Optional[str] = Form(None),
    target_language: Optional[str] = Form(None),
):
    if not (capcut_en.filename or "").lower().endswith(".srt"):
        raise HTTPException(422, "补充源语言字幕必须是 .srt 文件")
    path = await persist_upload(capcut_en)
    try:
        language_pair = None
        source_value = _form_value(source_language)
        target_value = _form_value(target_language)
        if source_value is not None or target_value is not None:
            existing = manager.get(job_id).get("options", {})
            language_pair = _validate_language_pair(
                source_value if source_value is not None else existing.get("source_language"),
                target_value if target_value is not None else existing.get("target_language"),
            )
        return _public_payload(
            manager.attach_capcut(job_id, path, language_pair=language_pair)
        )
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/jobs/{job_id}/start")
async def start_translation(
    job_id: str,
    model: Optional[str] = Form(None),
    round2_model: Optional[str] = Form(None),
    api_key: str = Form(""),
    api_key_storage: str = Form("secure"),
    base_url: Optional[str] = Form(None),
    proxy: Optional[str] = Form(None),
    batch_size: Optional[int] = Form(None),
    game: Optional[str] = Form(None),
    local_whisper: Optional[bool] = Form(None),
    burn_after_translation: Optional[bool] = Form(None),
    dry_run: Optional[bool] = Form(None),
    source_language: Optional[str] = Form(None),
    target_language: Optional[str] = Form(None),
):
    try:
        existing = manager.get(job_id).get("options", {})
        saved = _translation_settings_snapshot()
        dry_run_value = (
            bool(_form_value(dry_run))
            if _form_value(dry_run) is not None
            else bool(existing.get("dry_run", False))
        )
        model_value = _clean_text(
            _form_value(model) if _form_value(model) is not None
            else existing.get("model") or saved["model"],
            "模型名称", required=False, limit=200,
        )
        if not model_value:
            raise HTTPException(422, "请输入模型名称")
        round2_value = _clean_text(
            _form_value(round2_model) if _form_value(round2_model) is not None
            else existing.get("round2_model") or saved["round2_model"],
            "Round 2 模型名称", required=False, limit=200,
        )
        base_value = (
            _form_value(base_url) if _form_value(base_url) is not None
            else existing.get("base_url") or saved["base_url"]
        )
        proxy_value = (
            _form_value(proxy) if _form_value(proxy) is not None
            else existing.get("proxy", saved["proxy"])
        )
        batch_value = (
            _form_value(batch_size) if _form_value(batch_size) is not None
            else existing.get("batch_size", saved["batch_size"])
        )
        local_whisper_value = (
            bool(_form_value(local_whisper))
            if _form_value(local_whisper) is not None
            else bool(existing.get("local_whisper", saved["local_whisper"]))
        )
        burn_after_translation_value = (
            bool(_form_value(burn_after_translation))
            if _form_value(burn_after_translation) is not None
            else bool(existing.get("burn_after_translation", False))
        )
        game_value = (
            _form_value(game) if _form_value(game) is not None
            else existing.get("game", "wuwa")
        )
        options = {
            "model": model_value,
            "round2_model": round2_value,
            "base_url": _validate_base_url(
                base_value, required=not dry_run_value,
            ),
            "proxy": _validate_proxy(proxy_value),
            "batch_size": _validate_batch_size(batch_value),
            "game": _clean_text(game_value, "游戏", limit=50),
            "local_whisper": local_whisper_value,
            "burn_after_translation": burn_after_translation_value,
            "dry_run": dry_run_value,
        }
        source_value = _form_value(source_language)
        target_value = _form_value(target_language)
        if source_value is not None or target_value is not None:
            options.update(_validate_language_pair(
                source_value if source_value is not None else existing.get("source_language"),
                target_value if target_value is not None else existing.get("target_language"),
            ))
        _persist_user_settings(options)
        secret = _resolve_request_api_key(api_key, api_key_storage)
        if not options["dry_run"] and not secret:
            raise HTTPException(422, "请先配置 API Key")
        return _public_payload(manager.start_translation(
            job_id, secret, translation_options=options
        ))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs")
async def create_job(
    capcut_en: Optional[UploadFile] = File(None),
    whisper_en: Optional[UploadFile] = File(None),
    primary_srt: Optional[UploadFile] = File(None),
    secondary_srt: Optional[UploadFile] = File(None),
    video: Optional[UploadFile] = File(None),
    source_language: str = Form("en"),
    target_language: str = Form("zh-CN"),
):
    primary = primary_srt if hasattr(primary_srt, "filename") else capcut_en
    secondary = secondary_srt if hasattr(secondary_srt, "filename") else whisper_en
    if primary is None or secondary is None:
        raise HTTPException(422, "必须同时提供主字幕和辅助字幕")
    capcut_name = primary.filename or ""
    whisper_name = secondary.filename or ""
    if not capcut_name.lower().endswith(".srt") or not whisper_name.lower().endswith(".srt"):
        raise HTTPException(422, "主字幕和辅助字幕必须是 .srt 文件")
    if video and video.filename and Path(video.filename).suffix.lower() not in MEDIA_SUFFIXES:
        raise HTTPException(422, "视频或音频文件格式不受支持")
    files: dict[str, Path] = {}
    language_pair = _validate_language_pair(source_language, target_language)
    try:
        files["capcut_en"] = await persist_upload(primary)
        files["whisper_en"] = await persist_upload(secondary)
        if video and video.filename:
            files["video"] = await persist_upload(video)
        inherited = _translation_settings_snapshot()
        return _public_payload(manager.create(
            files, options={**inherited, **language_pair}
        ))
    except ValueError as error:
        for path in files.values():
            path.unlink(missing_ok=True)
        raise HTTPException(422, _safe_validation_detail(error))
    except BaseException:
        for path in files.values():
            path.unlink(missing_ok=True)
        raise


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str, after: int = 0):
    after = max(0, after)
    try:
        job = manager.get(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    logs = job.get("logs", [])
    selected = [event for index, event in enumerate(logs, start=1)
                if int(event.get("seq", index)) > after]
    next_seq = max((int(event.get("seq", index))
                    for index, event in enumerate(logs, start=1)), default=after)
    return {"events": selected, "next": next_seq}


@app.get("/api/jobs/{job_id}/risks")
def risks(job_id: str):
    try:
        return manager.risks(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")


@app.patch("/api/jobs/{job_id}/risks/{key}")
async def update_risk(job_id: str, key: str, payload: dict):
    try:
        return _public_payload(manager.update_risk(job_id, key, payload))
    except FileNotFoundError:
        raise HTTPException(404, "Risk queue not available")
    except KeyError:
        raise HTTPException(404, "Risk item not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/risks/bulk")
async def bulk_update_risks(job_id: str, payload: dict):
    try:
        return _public_payload(manager.bulk_update_risks(
            job_id, payload.get("items", []),
        ))
    except FileNotFoundError:
        raise HTTPException(404, "Risk queue not available")
    except KeyError:
        raise HTTPException(404, "Risk item not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/risks/{key}/round2")
def generate_round2_suggestion(
    job_id: str,
    key: str,
    api_key: str = Form(""),
):
    try:
        secret = _resolve_request_api_key(api_key)
        if not secret:
            raise HTTPException(422, "请先配置 API Key")
        return manager.generate_round2_suggestion(job_id, key, secret)
    except FileNotFoundError:
        raise HTTPException(404, "Round 2 state is unavailable")
    except KeyError:
        raise HTTPException(404, "Risk item not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.get("/api/jobs/{job_id}/subtitle-preview/{name}")
def subtitle_preview(job_id: str, name: str):
    if name not in {"final.zh.srt", "final.reviewed.zh.srt"}:
        raise HTTPException(404, "字幕预览不存在")
    try:
        path = manager.artifact_path(job_id, name)
        from pipeline.parser.srt_parser import parse_srt
        cues = parse_srt(str(path))
    except (FileNotFoundError, ValueError, UnicodeError):
        raise HTTPException(404, "字幕预览不存在") from None
    return {
        "name": name,
        "total": len(cues),
        "cues": [
            {"id": cue.id, "start": cue.start, "text": cue.text}
            for cue in cues[:8]
        ],
    }


@app.get("/api/jobs/{job_id}/artifacts/{name}")
def artifact(job_id: str, name: str):
    if Path(name).name != name:
        raise HTTPException(400, "Invalid artifact name")
    try:
        path = manager.artifact_path(job_id, name)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "Artifact not found")
    return FileResponse(path, filename=name)


@app.get("/api/job-events/stream")
async def stream_job_changes():
    """Push secret-free add/update/delete hints for every local task."""
    if not _acquire_sse_slot():
        raise HTTPException(429, "实时连接已达到本机上限，请稍后重试")

    async def generate():
        previous = manager.job_change_snapshot()
        yield f"event: ready\ndata: {json.dumps({'count': len(previous)})}\n\n"
        ticks = 0
        while True:
            current = manager.job_change_snapshot()
            all_ids = sorted(set(previous) | set(current))
            for job_id in all_ids:
                before = previous.get(job_id)
                after = current.get(job_id)
                if before == after:
                    continue
                details = after or before or {}
                event_type = (
                    "job_added" if before is None else
                    "job_deleted" if after is None else
                    "job_updated"
                )
                payload = {
                    "type": event_type,
                    "job_id": job_id,
                    "generation": int(details.get("generation", 0) or 0),
                    "updated_at": str(details.get("updated_at") or ""),
                }
                yield f"event: job\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            previous = current
            ticks += 1
            if ticks % 30 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    async def limited_generate():
        try:
            async for chunk in generate():
                yield chunk
        finally:
            _release_sse_slot()

    return StreamingResponse(
        limited_generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/stream")
async def stream_events(job_id: str, after: int = 0):
    """SSE endpoint: push new log entries and status changes in real time."""
    if not _acquire_sse_slot():
        raise HTTPException(429, "实时连接已达到本机上限，请稍后重试")

    async def generate():
        cursor = after
        last_status = None
        last_progress = None
        try:
            job = manager.get(job_id)
        except FileNotFoundError:
            yield f"event: error\ndata: {json.dumps({'error': 'Task not found'}, ensure_ascii=False)}\n\n"
            return
        last_status = job["status"]
        last_progress = job.get("progress")
        yield f"event: status\ndata: {json.dumps({'status': job['status'], 'stage': job['stage']}, ensure_ascii=False)}\n\n"
        yield f"event: progress\ndata: {json.dumps({'progress': last_progress, 'stage': job['stage'], 'elapsed_seconds': job.get('elapsed_seconds', 0)}, ensure_ascii=False)}\n\n"
        ticks = 0
        while True:
            try:
                job = manager.get(job_id)
            except FileNotFoundError:
                yield f"event: error\ndata: {json.dumps({'error': 'Task deleted'}, ensure_ascii=False)}\n\n"
                return
            logs = job.get("logs", [])
            new_logs = [event for index, event in enumerate(logs, start=1)
                        if int(event.get("seq", index)) > cursor]
            for log in new_logs:
                yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
            if new_logs:
                cursor = max(int(event.get("seq", cursor)) for event in new_logs)
            if job["status"] != last_status:
                last_status = job["status"]
                yield f"event: status\ndata: {json.dumps({'status': job['status'], 'stage': job['stage']}, ensure_ascii=False)}\n\n"
            if job.get("progress") != last_progress:
                last_progress = job.get("progress")
                yield f"event: progress\ndata: {json.dumps({'progress': last_progress, 'stage': job['stage'], 'elapsed_seconds': job.get('elapsed_seconds', 0)}, ensure_ascii=False)}\n\n"
            if job["status"] in (
                "waiting_capcut", "ready_for_translation",
                "paused", "completed", "failed", "cancelled",
            ):
                yield f"event: done\ndata: {json.dumps({'status': job['status']}, ensure_ascii=False)}\n\n"
                return
            ticks += 1
            if ticks % 30 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    async def limited_generate():
        try:
            async for chunk in generate():
                yield chunk
        finally:
            _release_sse_slot()

    return StreamingResponse(
        limited_generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/retry")
async def retry_failed_job(
    job_id: str,
    api_key: str = Form(""),
    api_key_storage: str = Form("secure"),
    model: str = Form(""),
    round2_model: str = Form(""),
    base_url: Optional[str] = Form(None),
    proxy: Optional[str] = Form(None),
    batch_size: Optional[int] = Form(None),
    game: Optional[str] = Form(None),
    local_whisper: Optional[bool] = Form(None),
    burn_after_translation: Optional[bool] = Form(None),
    dry_run: Optional[bool] = Form(None),
    source_language: Optional[str] = Form(None),
    target_language: Optional[str] = Form(None),
    restart: bool = Form(False),
    youtube_auth: Optional[str] = Form(None),
    youtube_cookies_text: Optional[str] = Form(None),
):
    cookie_payload = None
    try:
        restart = bool(_form_value(restart))
        secret = _resolve_request_api_key(api_key, api_key_storage)
        model = _clean_text(model, "模型名称", required=False, limit=200)
        round2_model = _clean_text(
            round2_model, "Round 2 模型名称",
            required=False, limit=200,
        )
        options = {}
        if model:
            options["model"] = model
        base_url_value = _form_value(base_url)
        if base_url_value is not None:
            options["base_url"] = _validate_base_url(
                base_url_value, required=False,
            )
        proxy_value = _form_value(proxy)
        if proxy_value is not None:
            options["proxy"] = _validate_proxy(proxy_value)
        if round2_model:
            options["round2_model"] = round2_model
        if batch_size is not None:
            options["batch_size"] = _validate_batch_size(batch_size)
        if game is not None:
            options["game"] = _clean_text(game, "游戏", limit=50)
        if local_whisper is not None:
            options["local_whisper"] = bool(_form_value(local_whisper))
        if burn_after_translation is not None:
            options["burn_after_translation"] = bool(
                _form_value(burn_after_translation)
            )
        if dry_run is not None:
            options["dry_run"] = bool(_form_value(dry_run))
        source_value = _form_value(source_language)
        target_value = _form_value(target_language)
        if source_value is not None or target_value is not None:
            existing = manager.get(job_id).get("options", {})
            options.update(_validate_language_pair(
                source_value if source_value is not None else existing.get("source_language"),
                target_value if target_value is not None else existing.get("target_language"),
            ))
        if youtube_auth is not None:
            youtube_auth = str(youtube_auth).strip().lower()
            if youtube_auth not in {"none", "chrome", "file"}:
                raise HTTPException(422, "无效的 YouTube 认证方式")
        pasted_cookies = youtube_cookies_text or ""
        has_pasted = bool(pasted_cookies.strip())
        if youtube_auth == "file" and not has_pasted:
            raise HTTPException(422, "请粘贴 cookies.txt 内容")
        if youtube_auth != "file" and has_pasted:
            raise HTTPException(422, "粘贴 Cookie 时认证方式必须选择 cookies.txt")
        if has_pasted:
            cookie_payload = parse_youtube_cookie_text(pasted_cookies)
        if options:
            _persist_user_settings(options)
        return _public_payload(manager.retry_failed(
            job_id, secret, restart=restart,
            translation_options=options or None,
            youtube_auth=youtube_auth,
            youtube_cookie=cookie_payload,
        ))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))
    finally:
        if cookie_payload is not None:
            cookie_payload[:] = b"\0" * len(cookie_payload)
            cookie_payload.clear()

@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return _public_payload(manager.cancel(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    try:
        return _public_payload(manager.pause(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    try:
        return _public_payload(manager.resume(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))

@app.post("/api/jobs/{job_id}/force-reset")
def force_reset_job(job_id: str):
    try:
        return _public_payload(manager.force_reset(job_id))
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    try:
        return manager.delete(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Task not found")
    except ValueError as error:
        raise HTTPException(422, _safe_validation_detail(error))


def _batch_retry_profile(job: dict) -> tuple:
    options = job.get("options") or {}
    inputs = job.get("inputs") or {}
    is_download = bool(job.get("url")) and not (
        inputs.get("primary_srt") or inputs.get("capcut_en")
    )
    return (
        "download" if is_download else "translation",
        str(job.get("error_code") or ""),
        str(options.get("model") or ""),
        str(options.get("round2_model") or ""),
        str(options.get("base_url") or ""),
        str(options.get("proxy") or ""),
        str(options.get("source_language") or "en"),
        str(options.get("target_language") or "zh-CN"),
        bool(options.get("dry_run")),
    )


@app.post("/api/jobs/batch-retry")
def batch_retry_jobs(payload: dict):
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "缺少任务 ID 列表")
    ids = list(dict.fromkeys(str(item) for item in ids))
    if len(ids) > 20:
        raise HTTPException(422, "单次最多重试 20 个任务")

    jobs = []
    for job_id in ids:
        try:
            job = manager.get(job_id)
        except FileNotFoundError:
            raise HTTPException(422, "所选任务已不存在，请刷新列表")
        if job.get("status") != "failed":
            raise HTTPException(422, "批量重试只接受失败任务")
        jobs.append(job)

    profiles = {_batch_retry_profile(job) for job in jobs}
    if len(profiles) != 1:
        raise HTTPException(422, "所选任务的错误类型或翻译配置不同，请分组重试")
    profile = next(iter(profiles))
    is_download = profile[0] == "download"
    if is_download and any(
        job.get("error_code") == "youtube_auth_required"
        or str((job.get("options") or {}).get("youtube_auth")) == "file"
        for job in jobs
    ):
        raise HTTPException(422, "需要 Cookie 的下载任务请逐个更新认证后重试")
    needs_key = not is_download and not bool(profile[-1])
    secret = _resolve_request_api_key(
        payload.get("api_key", ""), payload.get("api_key_storage", "secure"),
    )
    if needs_key and not secret:
        raise HTTPException(422, "请先配置 API Key")
    restart = bool(payload.get("restart", False))
    result = {"queued": [], "errors": {}}
    for job_id in ids:
        try:
            manager.retry_failed(job_id, secret, restart=restart)
            result["queued"].append(job_id)
        except (FileNotFoundError, ValueError):
            result["errors"][job_id] = public_error_fields(
                "operation_blocked"
            )["message"]
    return result


@app.post("/api/jobs/batch-delete")
def batch_delete_jobs(payload: dict):
    """批量删除任务：复用单任务安全删除逻辑（running/queued 拒绝、后台停止拒绝、
    rmtree 重试、secrets/futures 清理）。逐任务独立错误处理，一个失败不阻塞其他。"""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "缺少任务 ID 列表")
    ids = [str(item) for item in ids]
    result = {"deleted": [], "blocked": [], "not_found": [], "errors": {}}
    for job_id in ids:
        try:
            deleted = manager.delete(job_id)
            if deleted.get("deleted"):
                result["deleted"].append(job_id)
            else:
                result["blocked"].append(job_id)
        except FileNotFoundError:
            result["not_found"].append(job_id)
        except ValueError as error:
            result["blocked"].append(job_id)
            result["errors"][job_id] = public_error_fields(
                "operation_blocked"
            )["message"]
    return result


@app.post("/api/jobs/batch-archive")
def batch_archive_jobs(payload: dict):
    """批量设置归档状态（元数据更新，不删文件）。"""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "缺少任务 ID 列表")
    archived = bool(payload.get("archived", True))
    ids = [str(item) for item in ids]
    result = {"updated": [], "not_found": [], "errors": {}}
    for job_id in ids:
        try:
            manager.update_metadata(job_id, archived=archived)
            result["updated"].append(job_id)
        except FileNotFoundError:
            result["not_found"].append(job_id)
        except ValueError as error:
            result["errors"][job_id] = public_error_fields(
                "metadata_update_error"
            )["message"]
    return result
