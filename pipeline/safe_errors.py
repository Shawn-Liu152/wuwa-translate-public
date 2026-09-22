"""Public error contract shared by the pipeline and local Web application.

Exception messages are private implementation details.  Only stable codes,
validated upstream status codes, and locally authored messages may cross into
logs, manifests, job JSON, or HTTP responses.
"""
from __future__ import annotations

import re
from typing import Any


PUBLIC_ERROR_MAX_CHARS = 240

_PUBLIC_MESSAGES = {
    "api_error": "模型接口请求失败，请重试",
    "authentication_error": "API Key 无效、已过期或无权访问当前模型",
    "quota_error": "API 账户额度不足，请充值后重试",
    "configuration_error": "模型接口配置无效，请检查接口地址和模型名称",
    "provider_unavailable": "模型接口暂时不可用，请稍后重试",
    "invalid_provider_response": "模型接口返回无效内容，请重试",
    "translation_batch_error": "翻译批次失败，请重试",
    "round2_invalid_response": "Round 2 返回无效结构化结果，请重试",
    "batch_processing_error": "批次处理失败，请重试",
    "pipeline_error": "翻译任务失败，请重试",
    "download_error": "素材下载或处理失败，请重试",
    "source_unreliable": "素材质量不足，请更换来源或重新上传",
    "youtube_auth_required": "YouTube 登录凭据不可用，请重新提供后重试",
    "operation_blocked": "任务当前状态不允许执行此操作",
    "metadata_update_error": "任务信息更新失败，请刷新后重试",
    "cancelled": "任务已取消",
    "interrupted": "服务重启中断了任务，请重新配置后重试",
    "internal_error": "操作失败，请重试",
}

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\b"
    r"\s*(?::|=)\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
_SECRET_PREFIX_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+)"
)
_POSIX_PERSONAL_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9:])/(?:home|users|root|tmp|private|workspace|var/folders)/"
)


def normalize_error_code(value: Any, *, fallback: str = "internal_error") -> str:
    """Return a bounded machine-readable code from the public allowlist."""
    candidate = str(value or "").strip().lower()
    if (
        _ERROR_CODE_RE.fullmatch(candidate)
        and candidate in _PUBLIC_MESSAGES
    ):
        return candidate
    return fallback if fallback in _PUBLIC_MESSAGES else "internal_error"


def normalize_upstream_status(value: Any) -> int | None:
    """Accept only real HTTP status integers; booleans are not status codes."""
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def public_error_fields(
    error_code: Any,
    upstream_status: Any = None,
    *,
    fallback_code: str = "internal_error",
) -> dict[str, object]:
    """Build the only error fields allowed across a persistence boundary."""
    code = normalize_error_code(error_code, fallback=fallback_code)
    status = normalize_upstream_status(upstream_status)
    message = _PUBLIC_MESSAGES[code]
    if status is not None:
        message = f"{message}（上游 HTTP {status}）"
    return {
        "error_code": code,
        "upstream_status": status,
        "message": message[:PUBLIC_ERROR_MAX_CHARS],
    }


def contains_sensitive_detail(value: Any) -> bool:
    """Detect credential-shaped values and personal absolute paths."""
    text = str(value or "")
    return any(pattern.search(text) for pattern in (
        _SENSITIVE_ASSIGNMENT_RE,
        _BEARER_RE,
        _SECRET_PREFIX_RE,
        _WINDOWS_ABSOLUTE_PATH_RE,
        _POSIX_PERSONAL_PATH_RE,
    ))


def safe_public_text(
    value: Any,
    *,
    fallback: str = "运行详情已隐藏，以防泄露敏感信息",
    limit: int = PUBLIC_ERROR_MAX_CHARS,
) -> str:
    """Bound a locally generated message and discard suspicious whole records.

    Redacting only the credential token is insufficient because a malicious
    provider can place private subtitle text beside it.  If a record contains
    any sensitive marker, the complete record is replaced with the fallback.
    """
    fallback = _CONTROL_RE.sub(" ", str(fallback or "操作失败，请重试"))
    fallback = " ".join(fallback.split())[:max(1, int(limit))]
    text = str(value or "")
    if contains_sensitive_detail(text):
        return fallback
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return fallback
    limit = max(1, min(int(limit), PUBLIC_ERROR_MAX_CHARS))
    if len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text
