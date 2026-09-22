"""Persistent local job manager for the subtitle web workspace."""
from __future__ import annotations

import json
import hashlib
import copy
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.config import Config, normalize_llm_base_url
from pipeline.languages import normalize_language_pair
from pipeline.pipeline_manifest import PipelineCancelled
from pipeline.pricing import estimate_model_cost
from pipeline.safe_errors import public_error_fields, safe_public_text
from pipeline.translate.llm import LLMAPIError
from web.private_cookie_store import (
    CookieCredentialUnavailable,
    PrivateCookieStore,
)
from web.job_change_cache import JobChangeCache
from web.job_storage import (
    atomic_json,
    now,
    parse_activity_timestamp as _parse_activity_timestamp,
)

ROOT = Path(__file__).resolve().parents[1]
JOBS_ROOT = ROOT / "download"
TRANSLATION_MEMORY_PATH = ROOT / "data" / "translation_memory.json"
JOBS_ROOT.mkdir(exist_ok=True)
LOG = logging.getLogger(__name__)
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$|^[A-Za-z0-9_-]{1,64}$")
DEFAULT_MAX_JOBS = 100
DEFAULT_MAX_JOB_DISK_BYTES = 10 * 1024 * 1024 * 1024


def _configured_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        LOG.warning("Invalid %s; using safe default", name)
        return default
    return value if value > 0 else default


MAX_JOBS = _configured_positive_int("SUBTITLE_MAX_JOBS", DEFAULT_MAX_JOBS)
MAX_JOB_DISK_BYTES = _configured_positive_int(
    "SUBTITLE_MAX_JOB_DISK_BYTES", DEFAULT_MAX_JOB_DISK_BYTES
)

# 看门狗配置统一由 pipeline.config.Config 解析和校验；保留模块级别名，
# 便于测试按单个 JobManager 场景临时覆盖阈值。
STUCK_JOB_TIMEOUT_MINUTES = Config.STUCK_JOB_TIMEOUT_MINUTES
STUCK_JOB_CHECK_INTERVAL_SECONDS = Config.STUCK_JOB_CHECK_INTERVAL_SECONDS

DEFAULT_TRANSLATION_OPTIONS: dict[str, Any] = {
    "model": "",
    "round2_model": "",
    "base_url": "",
    "proxy": "http://127.0.0.1:7890",
    "batch_size": 28,
    "game": "wuwa",
    "local_whisper": False,
    "dry_run": False,
    "burn_after_translation": False,
    "reference_strategy": "adaptive",
    "api_key_configured": False,
    "source_language": "en",
    "target_language": "zh-CN",
}

PROCESS_FILES_DIR = "过程文件"
DELIVERY_FILENAMES = {
    "job.json",
    "final.zh.srt",
    "final.reviewed.zh.srt",
    "final.zh.burned.mp4",
}
DELIVERY_MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
DELIVERY_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _sanitize_job_public_fields(job: dict) -> None:
    """Normalize errors before a job crosses a disk or API boundary."""
    if job.get("error") is not None:
        fields = public_error_fields(
            job.get("error_code"),
            job.get("upstream_status"),
        )
        job["error"] = fields["message"]
        job["error_code"] = fields["error_code"]
        job["upstream_status"] = fields["upstream_status"]
    elif job.get("error_code") is None:
        job["upstream_status"] = None
    for event in job.get("logs") or []:
        if not isinstance(event, dict) or "message" not in event:
            continue
        event["message"] = safe_public_text(
            event["message"],
            fallback=(
                "任务执行详情已隐藏，以防泄露敏感信息"
                if event.get("kind") == "error"
                else "运行步骤已更新"
            ),
        )


def _whisper_reference_plan(strategy: str, youtube_quality) -> tuple[bool, bool]:
    """Return whether Whisper should run and whether failure must stop the job."""
    usable = bool(getattr(youtube_quality, "usable", False))
    needs_secondary = bool(
        getattr(youtube_quality, "needs_secondary", False)
    )
    required = (
        strategy == "whisper"
        or (strategy == "adaptive" and not usable)
    )
    desired = required or (
        strategy == "adaptive" and usable and needs_secondary
    )
    return desired, required


def asr_runtime_plan(source_language: str, quality_priority: bool = False) -> dict[str, Any]:
    """Select a language-capable ASR model and silence policy."""
    source_language = normalize_language_pair({
        "source_language": source_language,
    })["source_language"]
    if source_language == "en":
        return {
            "model": "large-v3" if quality_priority else "distil-large-v3",
            "use_vad": False,
            "reason": "English-optimised Distil-Whisper" if not quality_priority else "quality-priority multilingual model",
        }
    return {
        "model": "large-v3" if quality_priority else "turbo",
        "use_vad": True,
        "reason": f"multilingual {source_language} recognition with silence filtering",
    }


def _source_inputs(
    primary: str | Path, secondary: str | Path, source_language: str,
) -> dict[str, str]:
    """Write language-neutral inputs while retaining English legacy aliases."""
    values = {
        "primary_srt": str(primary),
        "secondary_srt": str(secondary),
    }
    if source_language == "en":
        values.update({
            "capcut_en": str(primary),
            "whisper_en": str(secondary),
        })
    return values


def _source_input(job: dict, neutral_name: str, legacy_name: str) -> str:
    inputs = job.get("inputs") or {}
    return str(inputs.get(neutral_name) or inputs.get(legacy_name) or "")


def _assess_source(path: str | Path, source_language: str):
    """Keep old English test/integration hooks stable during migration."""
    from pipeline import source_quality
    if source_language == "en":
        return source_quality.assess_english_srt(path)
    return source_quality.assess_source_srt(path, source_language=source_language)


def _language_label(source_language: str) -> str:
    return {"en": "英语", "ja": "日语", "ko": "韩语"}.get(
        source_language, source_language,
    )


def _delivery_status(metrics: dict, *, dry_run: bool = False) -> str:
    if dry_run:
        return "automatic_complete"
    quality = metrics.get("quality") or {}
    risk_rate = float(quality.get("risk_rate", 0.0) or 0.0)
    required_rate = float(quality.get("review_required_rate", 0.0) or 0.0)
    required_count = int(quality.get("review_required_count", 0) or 0)
    if risk_rate >= 0.8 and required_rate >= 0.5:
        return "source_unreliable"
    if required_count:
        return "review_required"
    return "deliverable"


class JobManager:
    def __init__(self, private_cookie_store: PrivateCookieStore | None = None):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subtitle-job")
        self.lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._accepting_work = True
        self._closed = False
        self.secrets: dict[str, str] = {}
        self.futures: dict[str, Any] = {}
        self.burn_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="subtitle-burn"
        )
        self.burn_futures: dict[str, Any] = {}
        self._burn_fingerprint_cache: dict[str, tuple] = {}
        self._job_change_cache = JobChangeCache()
        self.burn_cancel_events: dict[str, threading.Event] = {}
        # job_id -> (generation, 告警事件 seq)。看门狗自己写的告警不能算心跳，
        # 否则告警一落地任务就"恢复活动"，永远升不到自动停止那一级。
        self._stuck_notices: dict[str, tuple[int, int]] = {}
        self.private_cookie_store = private_cookie_store or PrivateCookieStore(
            legacy_jobs_root=JOBS_ROOT
        )

    def initialize_secure_runtime(self) -> None:
        """Acquire the private credential store and purge crash leftovers."""
        self.private_cookie_store.initialize()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            with self.lock:
                self._accepting_work = False
                for event in self.burn_cancel_events.values():
                    event.set()
            try:
                self.burn_pool.shutdown(wait=True, cancel_futures=True)
                self.pool.shutdown(wait=True, cancel_futures=True)
            finally:
                try:
                    self.private_cookie_store.close()
                except Exception:
                    LOG.warning(
                        "Private credential cleanup did not fully complete; "
                        "details discarded"
                    )
                with self.lock:
                    self.secrets.clear()
                    self.futures.clear()
                    self.burn_futures.clear()
                    self.burn_cancel_events.clear()
                    self._stuck_notices.clear()
                    self._closed = True

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if not JOB_ID_RE.fullmatch(job_id or "") or job_id in {".", ".."}:
            raise FileNotFoundError(job_id)
        return job_id

    def _workspace(self, job_id: str) -> Path:
        return JOBS_ROOT / self._validate_job_id(job_id)

    def _path(self, job_id: str) -> Path:
        return self._workspace(job_id) / "job.json"

    def _disk_usage(self) -> int:
        total = 0
        try:
            workspaces = JOBS_ROOT.iterdir()
            for workspace in workspaces:
                if not workspace.is_dir() or workspace.is_symlink():
                    continue
                for path in workspace.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        try:
                            total += path.stat().st_size
                        except OSError:
                            continue
        except OSError:
            return total
        return total

    def disk_usage_report(self) -> dict:
        """Return job-root quota consumption exactly as the quota gate sees it."""
        used = self._disk_usage()
        return {
            "used_bytes": used,
            "limit_bytes": MAX_JOB_DISK_BYTES,
            "percent": round(used * 100.0 / MAX_JOB_DISK_BYTES, 1),
        }

    def _ensure_quota(
            self, *, additional_jobs: int = 0, incoming_bytes: int = 0,
    ) -> None:
        job_count = 0
        try:
            for workspace in JOBS_ROOT.iterdir():
                if workspace.is_dir() and (workspace / "job.json").is_file():
                    job_count += 1
        except OSError:
            pass
        if job_count + additional_jobs > MAX_JOBS:
            raise ValueError("任务数量已达到本机上限，请先整理或删除旧任务")
        if self._disk_usage() + max(0, int(incoming_bytes)) > MAX_JOB_DISK_BYTES:
            raise ValueError("任务文件占用空间已达到本机上限，请先整理旧任务")

    def _safe_job_path(self, job_id: str, value: str | Path) -> Path:
        workspace = self._workspace(job_id).resolve()
        raw = Path(value)
        candidate = (raw if raw.is_absolute() else workspace / raw).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("任务文件路径超出工作目录") from exc
        return candidate

    def _resolve_stored_input(self, job_id: str, value: str) -> str:
        """Re-anchor stored input paths whose file moved into 过程文件/.

        任务完成后的"从头重跑/继续失败批次"带着完成前保存的 inputs 进来；
        存量任务的存储往返可能丢失 过程文件/ 子目录（2026-09-06 任务
        6311f11a3cad 实测：重跑必 FileNotFoundError）。原路径存在时原样
        返回；仅当原路径缺失且 过程文件/ 下有同名文件时才回锚。
        """
        if not value:
            return value
        resolved = self._safe_job_path(job_id, value)
        if resolved.exists():
            return str(resolved)
        candidate = self._workspace(job_id) / PROCESS_FILES_DIR / Path(value).name
        if candidate.is_file():
            return str(candidate)
        return str(resolved)

    def _relative_job_path(self, job_id: str, value: str | Path) -> str:
        workspace = self._workspace(job_id).resolve()
        return self._safe_job_path(job_id, value).relative_to(workspace).as_posix()

    def _hydrate_job_paths(self, job_id: str, job: dict) -> None:
        for field in ("inputs", "artifacts"):
            values = job.get(field)
            if not isinstance(values, dict):
                continue
            job[field] = {
                name: (
                    str(self._safe_job_path(job_id, value))
                    if isinstance(value, str) and value else value
                )
                for name, value in values.items()
            }
        snapshot = job.get("config_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("path"), str):
            if snapshot["path"]:
                snapshot["path"] = str(
                    self._safe_job_path(job_id, snapshot["path"])
                )

    def _stored_job(self, job: dict) -> dict:
        job_id = self._validate_job_id(str(job.get("id", "")))
        stored = copy.deepcopy(job)
        stored["workspace"] = "."
        for field in ("review_pending_count", "burn_review_ready", "burned_video_current", "summary_only"):
            stored.pop(field, None)
        for field in ("inputs", "artifacts"):
            values = stored.get(field)
            if not isinstance(values, dict):
                continue
            stored[field] = {
                name: (
                    self._relative_job_path(job_id, value)
                    if isinstance(value, str) and value else value
                )
                for name, value in values.items()
            }
        snapshot = stored.get("config_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("path"), str):
            if snapshot["path"]:
                snapshot["path"] = self._relative_job_path(
                    job_id, snapshot["path"]
                )
        return stored

    def public_job(self, job: dict) -> dict:
        """Return an API-safe view whose file references are task-relative."""
        job_id = self._validate_job_id(str(job.get("id", "")))
        result = copy.deepcopy(job)
        result.pop("workspace", None)
        result.pop("_stage_checkpoints", None)
        for field in ("inputs", "artifacts"):
            values = result.get(field)
            if not isinstance(values, dict):
                continue
            result[field] = {
                name: (
                    self._relative_job_path(job_id, value)
                    if isinstance(value, str) and value else value
                )
                for name, value in values.items()
            }
        snapshot = result.get("config_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("path"), str):
            if snapshot["path"]:
                snapshot["path"] = self._relative_job_path(
                    job_id, snapshot["path"]
                )
        return result

    def public_payload(self, payload: Any) -> Any:
        """Convert job-bearing route results without exposing local paths."""
        if isinstance(payload, list):
            return [self.public_payload(item) for item in payload]
        if not isinstance(payload, dict):
            return payload
        result = copy.deepcopy(payload)
        if isinstance(result.get("job"), dict):
            result["job"] = self.public_job(result["job"])
            if isinstance(result.get("folder"), str):
                result["folder"] = self._relative_job_path(
                    result["job"]["id"], payload["folder"]
                )
        elif "id" in result:
            result = self.public_job(result)
        return result

    def _load(self, job_id: str) -> dict:
        job_id = self._validate_job_id(job_id)
        job = json.loads(self._path(job_id).read_text(encoding="utf-8"))
        if job.get("id") != job_id:
            raise ValueError("任务记录 ID 不匹配")
        stored_options = job.get("options") or {}
        if not isinstance(stored_options, dict):
            raise ValueError("任务语言配置无效")
        job["options"] = {
            **DEFAULT_TRANSLATION_OPTIONS,
            **stored_options,
            **normalize_language_pair(stored_options),
        }
        self._hydrate_job_paths(job_id, job)
        _sanitize_job_public_fields(job)
        job["workspace"] = str(self._workspace(job_id))
        return job

    def _save(self, job: dict) -> None:
        job_id = self._validate_job_id(str(job.get("id", "")))
        stored_options = job.get("options") or {}
        if not isinstance(stored_options, dict):
            raise ValueError("任务语言配置无效")
        job["options"] = {
            **DEFAULT_TRANSLATION_OPTIONS,
            **stored_options,
            **normalize_language_pair(stored_options),
        }
        _sanitize_job_public_fields(job)
        job["workspace"] = str(self._workspace(job_id))
        atomic_json(self._path(job["id"]), self._stored_job(job))
        self._job_change_cache.invalidate(job_id)

    def _require_accepting_work(self) -> None:
        if not self._accepting_work:
            raise RuntimeError("任务管理器正在关闭，不能接收新任务")

    def _submit(self, job_id: str, func, *args) -> Any:
        with self.lock:
            self._require_accepting_work()
            future = self.pool.submit(func, *args)
            if hasattr(future, "add_done_callback"):
                self.futures[job_id] = future

        if hasattr(future, "add_done_callback"):
            def forget(completed) -> None:
                with self.lock:
                    if self.futures.get(job_id) is completed:
                        self.futures.pop(job_id, None)

            future.add_done_callback(forget)
        return future

    def _submit_burn(self, job_id: str, func, *args) -> Any:
        with self.lock:
            self._require_accepting_work()
            future = self.burn_pool.submit(func, *args)
            if hasattr(future, "add_done_callback"):
                self.burn_futures[job_id] = future

        if hasattr(future, "add_done_callback"):
            def forget(completed) -> None:
                with self.lock:
                    if self.burn_futures.get(job_id) is completed:
                        self.burn_futures.pop(job_id, None)

            future.add_done_callback(forget)
        return future

    @staticmethod
    def _burn_export_active(job: dict) -> bool:
        export = (job.get("exports") or {}).get("burned_video") or {}
        return export.get("status") in {"queued", "running"}

    @staticmethod
    def _burn_input_fingerprint(video: Path, subtitle: Path) -> str:
        from pipeline.subtitle_burner import BLACK_OUTLINE_WHITE_2

        digest = hashlib.sha256()
        digest.update(BLACK_OUTLINE_WHITE_2.encode("ascii"))
        digest.update(b"\0h264-policy-v1\0")
        for path in (video, subtitle):
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _burn_export_current(self, job: dict) -> bool:
        """Validate a completed export without hashing large media on every poll."""
        export = (job.get("exports") or {}).get("burned_video") or {}
        if export.get("status") != "completed":
            return False
        artifacts = job.get("artifacts") or {}
        values = [artifacts.get("video"),
                  artifacts.get("final.reviewed.zh.srt") or artifacts.get("final.zh.srt"),
                  artifacts.get("final.zh.burned.mp4")]
        if not all(values):
            return False
        try:
            paths = [self._safe_job_path(job["id"], value) for value in values]
            if any(not path.is_file() or path.is_symlink() for path in paths):
                return False
            stats = [path.stat() for path in paths]
            if not stats[-1].st_size:
                return False
            signature = tuple((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                              for path, stat in zip(paths, stats))
            cached = self._burn_fingerprint_cache.get(job["id"])
            if cached is None or cached[0] != signature:
                cached = (signature, self._burn_input_fingerprint(paths[0], paths[1]))
                self._burn_fingerprint_cache[job["id"]] = cached
            return cached[1] == export.get("input_fingerprint")
        except (OSError, ValueError):
            return False

    def _review_gate_cleared(self, job: dict) -> bool:
        artifacts = job.get("artifacts") or {}
        reviewed_value = artifacts.get("final.reviewed.zh.srt")
        if not reviewed_value:
            return False
        reviewed = self._safe_job_path(job["id"], reviewed_value)
        if not reviewed.is_file() or reviewed.is_symlink():
            return False
        items = self.risks(job["id"])
        required = [item for item in items if item.get("review_required")]
        completed_states = {"auto_resolved", "resolved", "verified"}
        return bool(required) and all(
            item.get("state") in completed_states
            for item in required
        )

    def _defer_or_start_requested_burn(self, job_id: str) -> dict:
        from pipeline.subtitle_burner import BLACK_OUTLINE_WHITE_2

        with self.lock:
            job = self._load(job_id)
            if not bool((job.get("options") or {}).get("burn_after_translation")):
                return self._with_summary(job)
            if job.get("status") != "completed":
                return self._with_summary(job)
            ready = (
                job.get("delivery_status") == "deliverable"
                or self._review_gate_cleared(job)
            )
            if not ready:
                job.setdefault("exports", {})["burned_video"] = {
                    "requested": True,
                    "status": "pending",
                    "progress": 0,
                    "artifact": "final.zh.burned.mp4",
                    "style": BLACK_OUTLINE_WHITE_2,
                    "error_code": None,
                    "message": "等待完成字幕审校后自动生成",
                    "input_fingerprint": None,
                    "updated_at": now(),
                }
                job["updated_at"] = now()
                self._save(job)
                return self._with_summary(job)
        try:
            return self.request_burned_video(job_id)
        except ValueError as error:
            with self.lock:
                job = self._load(job_id)
                job.setdefault("exports", {})["burned_video"] = {
                    "requested": True,
                    "status": "failed",
                    "progress": 0,
                    "artifact": "final.zh.burned.mp4",
                    "style": BLACK_OUTLINE_WHITE_2,
                    "error_code": "burn_input_missing",
                    "message": safe_public_text(
                        str(error), fallback="生成带字幕视频所需的文件不存在"
                    ),
                    "input_fingerprint": None,
                    "updated_at": now(),
                }
                job["updated_at"] = now()
                self._save(job)
                return self._with_summary(job)

    def request_burned_video(self, job_id: str) -> dict:
        from pipeline.subtitle_burner import BLACK_OUTLINE_WHITE_2

        with self.lock:
            self._require_accepting_work()
            job = self._load(job_id)
            if job.get("status") != "completed":
                raise ValueError("任务完成后才能生成带字幕视频")
            if bool((job.get("options") or {}).get("dry_run")):
                raise ValueError("试运行结果不能生成正式带字幕视频")
            required = [item for item in self.risks(job_id) if item.get("review_required")]
            unfinished = any(item.get("state") not in {"auto_resolved", "resolved", "verified"}
                             for item in required)
            if (unfinished or (job.get("delivery_status") != "deliverable"
                    and not self._review_gate_cleared(job))):
                raise ValueError("字幕尚未完成审校，不能生成正式带字幕视频")
            artifacts = dict(job.get("artifacts") or {})
            video_value = artifacts.get("video")
            if not video_value:
                raise ValueError("任务没有可用视频，无法生成带字幕视频")
            video = self._safe_job_path(job_id, video_value)
            if not video.is_file() or video.is_symlink():
                raise ValueError("任务没有可用视频，无法生成带字幕视频")
            subtitle_value = (
                artifacts.get("final.reviewed.zh.srt")
                or artifacts.get("final.zh.srt")
            )
            if not subtitle_value:
                raise ValueError("任务没有可交付中文字幕")
            subtitle = self._safe_job_path(job_id, subtitle_value)
            if not subtitle.is_file() or subtitle.is_symlink():
                raise ValueError("任务没有可交付中文字幕")
            fingerprint = self._burn_input_fingerprint(video, subtitle)
            current = (job.get("exports") or {}).get("burned_video") or {}
            if current.get("status") in {"queued", "running"}:
                return self._with_summary(job)
            artifact_name = "final.zh.burned.mp4"
            existing_value = artifacts.get(artifact_name)
            if (
                current.get("status") == "completed"
                and current.get("input_fingerprint") == fingerprint
                and existing_value
            ):
                existing = self._safe_job_path(job_id, existing_value)
                if existing.is_file() and not existing.is_symlink() and existing.stat().st_size:
                    return self._with_summary(job)
            export = {
                "requested": True,
                "status": "queued",
                "progress": 0,
                "artifact": artifact_name,
                "style": BLACK_OUTLINE_WHITE_2,
                "error_code": None,
                "message": None,
                "input_fingerprint": fingerprint,
                "updated_at": now(),
            }
            job.setdefault("exports", {})["burned_video"] = export
            job["updated_at"] = now()
            self._append_event(job, "info", "带字幕视频已排队（黑框白字2）")
            self._save(job)
            self.burn_cancel_events[job_id] = threading.Event()
            try:
                self._submit_burn(
                    job_id, self._run_burned_video, job_id, fingerprint,
                    video, subtitle,
                )
            except BaseException:
                failed = self._load(job_id)
                failed_export = failed.setdefault("exports", {}).setdefault(
                    "burned_video", export
                )
                failed_export.update(
                    status="failed", error_code="burn_failed",
                    message="生成带字幕视频失败，可稍后重试",
                    updated_at=now(),
                )
                self._save(failed)
                self.burn_cancel_events.pop(job_id, None)
                raise
            return self.get(job_id)

    def _run_burned_video(
        self, job_id: str, fingerprint: str, video: Path, subtitle: Path,
    ) -> None:
        from pipeline import subtitle_burner

        output = self._workspace(job_id) / "final.zh.burned.mp4"
        with self.lock:
            job = self._load(job_id)
            export = (job.get("exports") or {}).get("burned_video") or {}
            if (export.get("input_fingerprint") != fingerprint
                    or export.get("status") != "queued"):
                return
            export.update(status="running", progress=0, updated_at=now())
            job["updated_at"] = now()
            self._append_event(job, "info", "正在生成带字幕视频")
            self._save(job)
            cancel_event = self.burn_cancel_events.setdefault(
                job_id, threading.Event()
            )

        def cancel_check() -> bool:
            if cancel_event.is_set():
                return True
            with self.lock:
                try:
                    current_job = self._load(job_id)
                except FileNotFoundError:
                    return True
                current = (current_job.get("exports") or {}).get("burned_video") or {}
                return (
                    current.get("input_fingerprint") != fingerprint
                    or current.get("status") != "running"
                )

        def progress(value: int) -> None:
            with self.lock:
                try:
                    current_job = self._load(job_id)
                except FileNotFoundError:
                    return
                current = (current_job.get("exports") or {}).get("burned_video") or {}
                if (current.get("input_fingerprint") != fingerprint
                        or current.get("status") != "running"):
                    return
                current["progress"] = max(
                    int(current.get("progress", 0) or 0),
                    max(0, min(100, int(value))),
                )
                current["updated_at"] = now()
                current_job["updated_at"] = now()
                self._save(current_job)

        try:
            result = subtitle_burner.burn_subtitles(
                subtitle_burner.BurnRequest(video, subtitle, output),
                on_progress=progress,
                cancel_check=cancel_check,
            )
            with self.lock:
                job = self._load(job_id)
                export = (job.get("exports") or {}).get("burned_video") or {}
                if (export.get("input_fingerprint") != fingerprint
                        or export.get("status") != "running"):
                    return
                job.setdefault("artifacts", {})[output.name] = str(result.output_path)
                export.update(
                    status="completed", progress=100, error_code=None,
                    message=None, encoder=result.encoder,
                    duration_seconds=result.duration_seconds,
                    updated_at=now(),
                )
                job["updated_at"] = now()
                self._append_event(job, "info", "带字幕视频已生成")
                self._save(job)
        except subtitle_burner.BurnError as error:
            with self.lock:
                try:
                    job = self._load(job_id)
                except FileNotFoundError:
                    return
                export = (job.get("exports") or {}).get("burned_video") or {}
                if export.get("input_fingerprint") != fingerprint:
                    return
                export.update(
                    status=("cancelled" if error.error_code == "burn_cancelled" else "failed"),
                    error_code=error.error_code, message=str(error), updated_at=now(),
                )
                job["updated_at"] = now()
                self._append_event(job, "error", str(error))
                self._save(job)
        except Exception as error:
            LOG.warning(
                "subtitle burn failed for job %s (%s); details discarded",
                job_id, type(error).__name__,
            )
            with self.lock:
                try:
                    job = self._load(job_id)
                except FileNotFoundError:
                    return
                export = (job.get("exports") or {}).get("burned_video") or {}
                if export.get("input_fingerprint") != fingerprint:
                    return
                export.update(
                    status="failed", error_code="burn_failed",
                    message="生成带字幕视频失败，可稍后重试",
                    updated_at=now(),
                )
                job["updated_at"] = now()
                self._append_event(job, "error", export["message"])
                self._save(job)
        finally:
            with self.lock:
                self.burn_cancel_events.pop(job_id, None)

    def cancel_burned_video(self, job_id: str) -> dict:
        with self.lock:
            job = self._load(job_id)
            export = (job.get("exports") or {}).get("burned_video") or {}
            if export.get("status") not in {"queued", "running"}:
                raise ValueError("当前没有正在生成的带字幕视频")
            event = self.burn_cancel_events.setdefault(job_id, threading.Event())
            event.set()
            export.update(
                status="cancelled", error_code="burn_cancelled",
                message="已取消生成带字幕视频", updated_at=now(),
            )
            job["updated_at"] = now()
            self._append_event(job, "info", export["message"])
            self._save(job)
            future = self.burn_futures.get(job_id)
            if future is not None:
                future.cancel()
            return self._with_summary(job)

    def _forget_secret(self, job_id: str, generation: int | None = None) -> None:
        with self.lock:
            self.secrets.pop(job_id, None)
            try:
                job = self._load(job_id)
            except FileNotFoundError:
                return
            if generation is not None and job.get("generation", 0) != generation:
                return
            options = dict(job.get("options", {}))
            options["api_key_configured"] = False
            job["options"] = options
            job["updated_at"] = now()
            self._save(job)

    def recover_interrupted_jobs(self) -> None:
        self.initialize_secure_runtime()
        for path in JOBS_ROOT.glob("*/job.json"):
            try:
                job = self._load(path.parent.name)
                changed = False
                export = (job.get("exports") or {}).get("burned_video") or {}
                if export.get("status") in {"running", "queued"}:
                    export.update(
                        status="failed",
                        error_code="burn_interrupted",
                        message="服务重启中断了视频生成，可重新生成",
                        updated_at=now(),
                    )
                    job["updated_at"] = now()
                    self._append_event(
                        job, "error", "服务重启中断了带字幕视频生成，可重新生成"
                    )
                    changed = True
                if job.get("status") in {"running", "queued"}:
                    needs_cookie = (
                        bool(job.get("url"))
                        and str(job.get("options", {}).get("youtube_auth", "none"))
                        == "file"
                    )
                    job["status"] = "failed"
                    job["stage"] = "Interrupted by service restart"
                    job["error"] = (
                        "服务重启已安全销毁临时 YouTube Cookie，请重新提供后重试"
                        if needs_cookie else
                        "服务重启中断了任务，请重新输入 API Key 后续传"
                    )
                    job["error_code"] = (
                        "youtube_auth_required" if needs_cookie else "interrupted"
                    )
                    job["generation"] = job.get("generation", 0) + 1
                    job.setdefault("options", {})["api_key_configured"] = False
                    job["updated_at"] = now()
                    self._append_event(
                        job,
                        "error",
                        "服务重启已销毁临时 Cookie，请重新提供后重试"
                        if needs_cookie else
                        "服务重启中断了任务，可从失败批次继续",
                    )
                    changed = True
                if changed:
                    self._save(job)
            except (OSError, ValueError, json.JSONDecodeError):
                LOG.warning("Could not recover one interrupted job record")

    def _job_last_activity(
        self, job: dict, ignore_seq: int | None = None,
    ) -> datetime | None:
        """Return the newest observable activity stamp for a job record.

        ``updated_at`` only moves on state transitions, so a healthy long
        download would look stale on its own.  Log events are the real
        heartbeat: download progress lines and the pipeline log handler both
        append them.  The watchdog's own warning is excluded by seq, otherwise
        it would masquerade as a heartbeat and block escalation.
        """
        stamps: list[datetime] = []
        updated = _parse_activity_timestamp(job.get("updated_at"))
        if updated is not None:
            stamps.append(updated)
        for event in job.get("logs") or []:
            if not isinstance(event, dict):
                continue
            if ignore_seq is not None and event.get("seq") == ignore_seq:
                continue
            stamp = _parse_activity_timestamp(event.get("at"))
            if stamp is not None:
                stamps.append(stamp)
        return max(stamps) if stamps else None

    def _stuck_candidates(self) -> list[tuple[str, dict, float, bool]]:
        """Return ``(job_id, job, silent_seconds, worker_alive)`` when stale.

        A ``queued`` job whose future is still pending is waiting for the
        single worker, which is normal rather than stuck, so it is skipped.
        """
        with self.lock:
            if self._closed:
                return []
        threshold = STUCK_JOB_TIMEOUT_MINUTES * 60.0
        reference = datetime.now().astimezone()
        findings: list[tuple[str, dict, float, bool]] = []
        for path in JOBS_ROOT.glob("*/job.json"):
            job_id = path.parent.name
            try:
                job = self._load(job_id)
            except (OSError, ValueError, json.JSONDecodeError):
                LOG.warning("Stuck-job scan could not read one job record")
                continue
            if job.get("status") not in {"running", "queued"}:
                continue
            with self.lock:
                future = self.futures.get(job_id)
                notice = self._stuck_notices.get(job_id)
            worker_alive = future is not None and not future.done()
            if job.get("status") == "queued" and worker_alive:
                continue
            last = self._job_last_activity(job, notice[1] if notice else None)
            if last is None:
                continue
            silent = (reference - last).total_seconds()
            if silent < threshold:
                continue
            findings.append((job_id, job, silent, worker_alive))
        return findings

    def _mark_job_stuck_failed(
        self, job_id: str, *, stage: str, event_message: str,
    ) -> dict | None:
        """Move a stuck job to failed and let its worker unwind cooperatively.

        Bumping ``generation`` *is* the stop signal: the worker's
        ``cancel_check()`` notices on its next poll, terminates the child
        process and cleans up after itself instead of being killed outright.
        """
        with self.lock:
            try:
                job = self._load(job_id)
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            if job.get("status") not in {"running", "queued"}:
                return None
            fields = public_error_fields("interrupted")
            job["status"] = "failed"
            job["stage"] = stage
            job["error"] = fields["message"]
            job["error_code"] = fields["error_code"]
            job["upstream_status"] = fields["upstream_status"]
            job["generation"] = job.get("generation", 0) + 1
            job.setdefault("options", {})["api_key_configured"] = False
            job["updated_at"] = now()
            self._append_event(job, "error", event_message)
            self._save(job)
            self.secrets.pop(job_id, None)
            self._stuck_notices.pop(job_id, None)
            self.private_cookie_store.discard(job_id)
            future = self.futures.get(job_id)
            if future is not None:
                future.cancel()
            return job

    def _warn_stuck_job(self, job_id: str, generation: int, silent: float) -> None:
        """Append one visible warning without touching the job's status."""
        with self.lock:
            try:
                job = self._load(job_id)
            except (OSError, ValueError, json.JSONDecodeError):
                return
            if not self._is_current_job(job, generation):
                return
            event = self._append_event(
                job,
                "error",
                f"任务已 {int(silent // 60)} 分钟没有任何进展，疑似卡死；"
                "已请求协作式取消；检测到执行线程仍活动，因此不会强制重置状态",
            )
            self._save(job)
            self._stuck_notices[job_id] = (generation, int(event["seq"]))

    def reset_stuck_jobs(self) -> list[dict]:
        """Reset orphaned jobs and request cancellation of live stale jobs.

        A live future is never forced into a terminal state: ``Future.cancel``
        merely requests cancellation and normally returns ``False`` once running.
        Only an orphaned or finished future is persisted as ``interrupted``.
        """
        threshold = STUCK_JOB_TIMEOUT_MINUTES * 60.0
        actions: list[dict] = []
        still_stuck: set[str] = set()
        for job_id, job, silent, worker_alive in self._stuck_candidates():
            generation = int(job.get("generation", 0))
            minutes = int(silent // 60)
            if not worker_alive:
                reset = self._mark_job_stuck_failed(
                    job_id,
                    stage="Reset by stuck-job watchdog",
                    event_message=(
                        "任务长时间没有进展且执行线程已不在，已重置为失败；"
                        "可从失败批次继续"
                    ),
                )
                if reset is not None:
                    actions.append({
                        "job_id": job_id,
                        "action": "reset",
                        "silent_minutes": minutes,
                    })
                continue
            still_stuck.add(job_id)
            with self.lock:
                notice = self._stuck_notices.get(job_id)
            if notice is None or notice[0] != generation:
                with self.lock:
                    future = self.futures.get(job_id)
                if future is not None:
                    future.cancel()
                self._warn_stuck_job(job_id, generation, silent)
                actions.append({
                    "job_id": job_id,
                    "action": "warned",
                    "silent_minutes": minutes,
                })
        with self.lock:
            for job_id in list(self._stuck_notices):
                if job_id not in still_stuck:
                    self._stuck_notices.pop(job_id, None)
        return actions

    def stuck_job_report(self) -> list[dict]:
        """Return non-sensitive findings about jobs that stopped progressing."""
        with self.lock:
            notices = dict(self._stuck_notices)
        report: list[dict] = []
        for job_id, job, silent, worker_alive in self._stuck_candidates():
            generation = int(job.get("generation", 0))
            notice = notices.get(job_id)
            report.append({
                "job_id": job_id,
                "status": str(job.get("status") or ""),
                "stage": safe_public_text(
                    job.get("stage") or "", fallback="运行步骤已更新",
                ),
                "silent_minutes": int(silent // 60),
                "worker_alive": worker_alive,
                "warned": bool(notice and notice[0] == generation),
            })
        return report

    @staticmethod
    def _append_event(job: dict, kind: str, message: str) -> dict:
        logs = job.setdefault("logs", [])
        if "next_event_seq" not in job:
            for index, existing in enumerate(logs, start=1):
                existing.setdefault("seq", index)
            job["next_event_seq"] = max(
                (int(event.get("seq", 0)) for event in logs), default=0
            ) + 1
        sequence = int(job["next_event_seq"])
        message = safe_public_text(
            message,
            fallback=(
                "任务执行详情已隐藏，以防泄露敏感信息"
                if kind == "error" else "运行步骤已更新"
            ),
        )
        event = {"seq": sequence, "at": now(), "kind": kind, "message": message}
        job["next_event_seq"] = sequence + 1
        logs.append(event)
        job["logs"] = logs[-300:]
        return event

    def _event(self, job_id: str, kind: str, message: str) -> None:
        with self.lock:
            job = self._load(job_id)
            self._append_event(job, kind, message)
            self._save(job)

    def _event_if_current(self, job_id: str, generation: int,
                          kind: str, message: str) -> bool:
        with self.lock:
            try:
                job = self._load(job_id)
            except FileNotFoundError:
                return False
            if not self._is_current_job(job, generation):
                return False
            self._append_event(job, kind, message)
            self._save(job)
            return True

    def _pipeline_log_handler(self, job_id: str, generation: int) -> logging.Handler:
        manager = self
        pipeline_logger_names = (
            "main",
            "long_video",
            "httpx",
            "scripts.whisper_transcribe",
        )

        class JobLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                # Batch translation runs in child threads. Filtering by logger
                # keeps those records while excluding unrelated Web requests.
                if not any(
                    record.name == name or record.name.startswith(name + ".")
                    for name in pipeline_logger_names
                ):
                    return
                try:
                    message = record.getMessage().strip()
                    if message:
                        manager._event_if_current(
                            job_id, generation,
                            "error" if record.levelno >= logging.ERROR else "info",
                            message[:1200],
                        )
                except Exception:
                    # Logging must never break the pipeline it is observing.
                    return

        handler = JobLogHandler(level=logging.INFO)
        return handler

    @staticmethod
    def _pipeline_failure_message(output: Path, status: int) -> str:
        """Return the most actionable persisted error for a failed pipeline."""
        candidates = (
            ("Round 2", output.with_name("final.round2.zh.srt.manifest.json")),
            ("Round 1", output.with_name("final.round1.zh.srt.manifest.json")),
        )
        for stage, manifest_path in candidates:
            if not manifest_path.exists():
                continue
            try:
                from pipeline.pipeline_manifest import load_manifest

                manifest = load_manifest(str(manifest_path))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            for batch_id, batch in (manifest.get("batches") or {}).items():
                if batch.get("state") != "failed" or not (
                    batch.get("error") or batch.get("error_code")
                ):
                    continue
                try:
                    display_batch = str(int(batch_id) + 1)
                except (TypeError, ValueError):
                    display_batch = str(batch_id)
                fields = public_error_fields(
                    batch.get("error_code"),
                    batch.get("upstream_status"),
                    fallback_code="batch_processing_error",
                )
                return f"{stage} 第 {display_batch} 批失败：{fields['message']}"
        fields = public_error_fields("pipeline_error")
        return f"{fields['message']}（退出状态 {int(status)}）"

    @staticmethod
    def _stage_config_hash(config: dict) -> str:
        encoded = json.dumps(
            config, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _stage_artifact_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _record_stage_checkpoint(
        self, job_id: str, generation: int, stage: str, config: dict,
        artifacts: dict[str, str],
    ) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", stage):
            raise ValueError("invalid checkpoint stage")
        with self.lock:
            job = self._load(job_id)
            if not self._is_current_job(job, generation):
                return False
            recorded: dict[str, dict] = {}
            for name, value in sorted(artifacts.items()):
                try:
                    path = self._safe_job_path(job_id, value)
                except (OSError, ValueError):
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                recorded[name] = {
                    "path": self._relative_job_path(job_id, path),
                    "size": stat.st_size,
                    "sha256": self._stage_artifact_hash(path),
                }
            if not recorded:
                return False
            checkpoints = dict(job.get("_stage_checkpoints") or {})
            checkpoints[stage] = {
                "version": 1,
                "config_hash": self._stage_config_hash(config),
                "artifacts": recorded,
                "completed_at": now(),
            }
            job["_stage_checkpoints"] = checkpoints
            job["updated_at"] = now()
            self._save(job)
            return True

    def _reuse_stage_checkpoint(
        self, job_id: str, stage: str, config: dict,
    ) -> dict[str, str] | None:
        with self.lock:
            job = self._load(job_id)
            checkpoint = (job.get("_stage_checkpoints") or {}).get(stage)
            if not isinstance(checkpoint, dict):
                return None
            if checkpoint.get("version") != 1:
                return None
            if checkpoint.get("config_hash") != self._stage_config_hash(config):
                return None
            recorded = checkpoint.get("artifacts")
            if not isinstance(recorded, dict) or not recorded:
                return None
            resolved: dict[str, str] = {}
            for name, evidence in recorded.items():
                if not isinstance(evidence, dict):
                    return None
                try:
                    path = self._safe_job_path(job_id, evidence.get("path", ""))
                except (OSError, ValueError):
                    return None
                if not path.is_file() or path.is_symlink():
                    return None
                if path.stat().st_size != int(evidence.get("size", -1)):
                    return None
                if self._stage_artifact_hash(path) != evidence.get("sha256"):
                    return None
                resolved[str(name)] = str(path.resolve())
            return resolved

    def job_change_snapshot(self) -> dict[str, dict]:
        """Share secret-free revisions across local event stream connections."""
        return self._job_change_cache.snapshot(JOBS_ROOT, self._load)

    def list(self) -> list[dict]:
        jobs = []
        for path in JOBS_ROOT.glob("*/job.json"):
            try:
                jobs.append(self._with_summary(self._load(path.parent.name), detailed=False))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda job: job["created_at"], reverse=True)

    def get(self, job_id: str, *, detailed: bool = True) -> dict:
        with self.lock:
            return self._with_summary(self._load(job_id), detailed=detailed)

    def _with_summary(self, job: dict, *, detailed: bool = True) -> dict:
        workspace = self._workspace(job["id"])
        try:
            disk_bytes = sum(
                path.stat().st_size for path in workspace.rglob("*") if path.is_file()
            ) if detailed else None
        except OSError:
            disk_bytes = 0
        result = dict(job)
        result["summary_only"] = not detailed
        if job.get("status") == "completed":
            try:
                required = [item for item in self.risks(job["id"]) if item.get("review_required")]
                result["review_pending_count"] = sum(
                    item.get("state") in {"pending", "review", "failed"} for item in required
                )
                result["burn_review_ready"] = not any(
                    item.get("state") not in {"auto_resolved", "resolved", "verified"}
                    for item in required
                ) and (job.get("delivery_status") == "deliverable" or self._review_gate_cleared(job))
            except (OSError, ValueError, TypeError, AttributeError):
                # Unreadable review evidence must block delivery, not the task list.
                result["review_pending_count"] = None
                result["burn_review_ready"] = False
            result["burned_video_current"] = self._burn_export_current(job) if detailed else False
        result["disk_bytes"] = disk_bytes
        try:
            created = datetime.fromisoformat(job["created_at"])
            updated = datetime.fromisoformat(
                job.get("completed_at") or job.get("updated_at") or job["created_at"]
            )
            result["elapsed_seconds"] = max(0, int((updated - created).total_seconds()))
        except (KeyError, TypeError, ValueError):
            result["elapsed_seconds"] = 0
        return result

    def update_metadata(self, job_id: str, *, custom_name: str | None = None,
                        archived: bool | None = None) -> dict:
        with self.lock:
            job = self._load(job_id)
            if custom_name is not None:
                job["custom_name"] = str(custom_name).strip()[:200]
            if archived is not None:
                job["archived"] = bool(archived)
            job["updated_at"] = now()
            self._save(job)
            return self._with_summary(job)

    def cleanup(self, job_id: str, kind: str) -> dict:
        with self.lock:
            job = self._load(job_id)
            if job["status"] in {"queued", "running"}:
                raise ValueError("任务运行中，不能清理文件")
            if self._burn_export_active(job):
                raise ValueError("带字幕视频生成中，不能清理文件")
            workspace = self._workspace(job_id).resolve()
            artifacts = dict(job.get("artifacts", {}))
            removed: list[str] = []
            for name, value in list(artifacts.items()):
                path = self._safe_job_path(job_id, value)
                remove = (
                    kind == "video" and name != "final.zh.burned.mp4" and (
                        name == "video" or path.suffix.lower() in {
                            ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
                            ".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg",
                        }
                    )
                ) or (
                    kind == "technical" and (
                        name == "metadata_json"
                        or path.suffix.lower() == ".json"
                        or name.endswith(".manifest.json")
                    )
                )
                if not remove or path.resolve() == (workspace / "job.json"):
                    continue
                path.unlink(missing_ok=True)
                artifacts.pop(name, None)
                removed.append(name)
            job["artifacts"] = artifacts
            job["updated_at"] = now()
            kind_label = "源媒体" if kind == "video" else "技术"
            self._append_event(job, "info", f"已清理 {len(removed)} 个{kind_label}文件")
            self._save(job)
            return {"ok": True, "removed": removed, "job": self._with_summary(job)}

    @staticmethod
    def _move_with_handle_retry(source: Path, target: Path) -> None:
        """shutil.move with a bounded retry for Windows file-handle lag.

        Windows 上 SQLite/日志句柄的最终释放依赖 GC：真实翻译刚写完
        config/<generation>/glossary.db 就整理文件时，移动目录可能瞬时
        WinError 32。gc.collect() 触发引用回收后短退避重试（与 delete
        的既有模式一致），重试耗尽仍失败才上抛。
        """
        import gc

        for attempt in range(4):
            try:
                shutil.move(str(source), str(target))
                return
            except PermissionError:
                if attempt >= 3:
                    raise
                gc.collect()
                time.sleep(0.2 * (attempt + 1))

    def _organize_technical_files(self, job: dict) -> dict:
        """Move non-delivery files below one folder and update their references."""
        job_id = self._validate_job_id(str(job["id"]))
        workspace = self._workspace(job_id).resolve()
        process_dir = workspace / PROCESS_FILES_DIR
        if process_dir.exists() and not process_dir.is_dir():
            raise ValueError(f"{PROCESS_FILES_DIR} 已存在但不是文件夹")

        artifacts = dict(job.get("artifacts", {}))
        preserved_paths: set[Path] = set()
        # 只保留最终交付文件（final SRT 等）。video/thumbnail 属于下载的
        # 中间产物，也归档进 过程文件/ —— artifacts 引用会在下方自动更新，
        # 因此 download 端点与 UI 预览仍可正常取用。
        # (原实现把 video/thumbnail 留在顶层，导致任务目录堆满大文件)

        moved_paths: dict[Path, Path] = {}
        moved_files: list[str] = []
        moved_directories: list[str] = []

        def delivery_file(path: Path) -> bool:
            return (
                path.name in DELIVERY_FILENAMES
                or path.resolve() in preserved_paths
                # 媒体/图片不再作为交付物保留——视频、封面等下载产物
                # 一律归档进 过程文件/，仅 final SRT 与 job.json 留在顶层。
            )

        def available_target(path: Path) -> Path:
            target = process_dir / path.name
            if not target.exists():
                return target
            index = 2
            while True:
                suffix = path.suffix if path.is_file() else ""
                stem = path.stem if path.is_file() else path.name
                candidate = process_dir / f"{stem} ({index}){suffix}"
                if not candidate.exists():
                    return candidate
                index += 1

        for path in sorted(workspace.iterdir(), key=lambda item: item.name.casefold()):
            if path == process_dir or delivery_file(path):
                continue
            is_directory = path.is_dir()
            source_path = path.resolve()
            target = available_target(path)
            process_dir.mkdir(exist_ok=True)
            self._move_with_handle_retry(path, target)
            moved_paths[source_path] = target.resolve()
            if is_directory:
                moved_directories.append(path.name)
            else:
                moved_files.append(path.name)

        if not moved_paths:
            return {
                "moved": [], "moved_directories": [],
                "folder": str(process_dir), "job": job,
            }

        def moved_value(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            try:
                return str(moved_paths.get(Path(value).resolve(), Path(value)))
            except OSError:
                return value

        job["artifacts"] = {
            name: moved_value(value) for name, value in artifacts.items()
        }
        job["inputs"] = {
            name: moved_value(value)
            for name, value in (job.get("inputs") or {}).items()
        }
        snapshot = dict(job.get("config_snapshot") or {})
        if snapshot.get("path"):
            snapshot["path"] = moved_value(snapshot["path"])
            job["config_snapshot"] = snapshot
        job["technical_files_folder"] = PROCESS_FILES_DIR
        return {
            "moved": moved_files,
            "moved_directories": moved_directories,
            "folder": str(process_dir),
            "job": job,
        }

    def organize(self, job_id: str) -> dict:
        """Group process artifacts while retaining every file for later review."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] in {"queued", "running"}:
                raise ValueError("任务运行中，暂时不能整理文件")
            if self._burn_export_active(job):
                raise ValueError("带字幕视频生成中，暂时不能整理文件")
            result = self._organize_technical_files(job)
            if result["moved"] or result["moved_directories"]:
                job["updated_at"] = now()
                self._append_event(
                    job,
                    "info",
                    "已整理过程文件："
                    f"移动 {len(result['moved'])} 个文件和 "
                    f"{len(result['moved_directories'])} 个文件夹",
                )
                self._save(job)
            result["job"] = self._with_summary(job)
            return result

    def create(self, inputs: dict[str, Path],
               options: dict[str, Any] | None = None, secret: str = "") -> dict:
        job_id = uuid.uuid4().hex[:12]
        workspace = self._workspace(job_id)
        normalized_options = {
            **DEFAULT_TRANSLATION_OPTIONS,
            **(options or {}),
            **normalize_language_pair(options or {}),
        }
        source_language = normalized_options["source_language"]
        destinations = {
            "capcut_en": f"capcut.{source_language}.srt",
            "whisper_en": f"whisper.{source_language}.srt",
        }
        try:
            from pipeline.parser.srt_parser import parse_srt
            for required in ("capcut_en", "whisper_en"):
                if required not in inputs or not parse_srt(str(inputs[required])):
                    raise ValueError(f"{required} 没有有效字幕")
            assessed_sources = {
                "capcut": _assess_source(
                    inputs["capcut_en"], source_language,
                ),
                "whisper": _assess_source(
                    inputs["whisper_en"], source_language,
                ),
            }
            mismatched_sources = [
                name for name, quality in assessed_sources.items()
                if quality.language_ratio < 0.45
            ]
            if mismatched_sources:
                labels = {
                    "capcut": "主字幕",
                    "whisper": "辅助字幕",
                }
                sources = "、".join(labels[name] for name in mismatched_sources)
                language_label = _language_label(source_language)
                raise ValueError(
                    f"{sources} 与声明的源语言不一致：任务要求 {language_label} SRT"
                )
            source_qualities = {
                name: quality.to_dict()
                for name, quality in assessed_sources.items()
            }

            incoming_bytes = sum(
                source.stat().st_size for source in inputs.values()
            )
            with self.lock:
                self._require_accepting_work()
                self._ensure_quota(additional_jobs=1, incoming_bytes=incoming_bytes)
                workspace.mkdir(parents=True)
                copied = {}
                artifacts = {}
                for name, source in inputs.items():
                    filename = destinations.get(name)
                    if filename is None:
                        suffix = source.suffix.lower()[:16]
                        filename = f"source{suffix}" if suffix else "source.media"
                    destination = workspace / filename
                    source.replace(destination)
                    copied[name] = str(destination)
                    artifacts[filename] = str(destination)
                copied.update(_source_inputs(
                    copied["capcut_en"], copied["whisper_en"], source_language,
                ))
                job = {
                    "id": job_id, "created_at": now(), "updated_at": now(),
                    "status": "ready_for_translation", "stage": "Sources ready",
                    "progress": {"completed": 0, "total": 0},
                    "inputs": copied,
                    "options": {**normalized_options,
                                "api_key_configured": bool(
                                    normalized_options.get("api_key_configured")
                                )},
                    "generation": 0,
                    "workspace": str(workspace), "artifacts": artifacts, "logs": [],
                    "source_qualities": source_qualities,
                    "next_event_seq": 1, "error": None, "error_code": None,
                }
                self._save(job)
                self._event(
                    job_id, "info",
                    f"{_language_label(source_language)}素材已准备完成，请配置并开始翻译",
                )
                return self.get(job_id)
        except BaseException:
            self.secrets.pop(job_id, None)
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    def create_from_url(
        self,
        url: str,
        options: dict[str, Any],
        youtube_cookie: bytes | bytearray | None = None,
    ) -> dict:
        job_id = uuid.uuid4().hex[:12]
        workspace = self._workspace(job_id)
        normalized_options = {
            **DEFAULT_TRANSLATION_OPTIONS,
            **options,
            **normalize_language_pair(options),
        }
        job = {
            "id": job_id, "created_at": now(), "updated_at": now(), "status": "queued",
            "stage": "Queued", "progress": {"completed": 0, "total": 0},
            "inputs": {}, "url": url,
            "options": {**normalized_options,
                        "api_key_configured": bool(
                            normalized_options.get("api_key_configured")
                        )},
            "generation": 0,
            "workspace": str(workspace), "artifacts": {}, "logs": [],
            "next_event_seq": 1, "error": None, "error_code": None,
        }
        try:
            with self.lock:
                self._require_accepting_work()
                self._ensure_quota(additional_jobs=1)
                workspace.mkdir(parents=True)
                self._save(job)
                if youtube_cookie is not None:
                    self.private_cookie_store.queue(job_id, 0, youtube_cookie)
                self._event(job_id, "info", "YouTube 下载已排队")
                self._submit(job_id, self._run_download, job_id, "", 0)
            return self.get(job_id)
        except BaseException:
            self.private_cookie_store.discard(job_id)
            self.secrets.pop(job_id, None)
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    @staticmethod
    def _is_current_job(job: dict, generation: int) -> bool:
        return (job.get("generation", 0) == generation
                and job.get("status") not in {
                    "paused", "cancelled", "failed", "completed",
                })

    def _is_current(self, job_id: str, generation: int) -> bool:
        with self.lock:
            try:
                job = self._load(job_id)
            except FileNotFoundError:
                return False
            return self._is_current_job(job, generation)

    def _update_if_current(self, job_id: str, generation: int, **changes: Any) -> dict | None:
        with self.lock:
            try:
                job = self._load(job_id)
            except FileNotFoundError:
                return None
            if not self._is_current_job(job, generation):
                return None
            job.update(changes)
            job["updated_at"] = now()
            self._save(job)
            return job

    @staticmethod
    def _download_stage_config(job: dict) -> dict:
        options = job.get("options") or {}
        return {
            "version": 1,
            "url": str(job.get("url") or ""),
            "proxy": str(options.get("proxy") or ""),
            "download_video": bool(options.get("download_video", True)),
            "download_subtitles": bool(options.get("download_subtitles", True)),
            "download_thumbnail": bool(options.get("download_thumbnail", True)),
            "video_quality": str(options.get("video_quality", "1080")),
            "source_language": normalize_language_pair(options)["source_language"],
            "reference_strategy": str(options.get("reference_strategy", "adaptive")),
        }

    def _automatic_source_state(
        self,
        job_id: str,
        job: dict,
        artifacts: dict,
        reference_source: str,
        source_language: str,
        youtube_source: str = "",
    ) -> dict:
        artifacts = dict(artifacts or {})
        youtube_source = str(
            youtube_source
            or artifacts.get("youtube_source")
            or artifacts.get(f"youtube_{source_language}")
            or artifacts.get("youtube_en")
            or ""
        )
        if reference_source == "whisper":
            primary = artifacts.get("whisper_source") or artifacts.get(
                f"whisper_{source_language}"
            )
            secondary = youtube_source
        else:
            primary = artifacts.get("youtube_aligned_source") or youtube_source
            secondary = artifacts.get("whisper_source") or artifacts.get(
                f"whisper_{source_language}"
            )
        if not primary:
            raise ValueError(
                f"没有获得可用的{_language_label(source_language)}字幕"
            )
        if (
            not secondary
            or Path(primary).resolve() == Path(secondary).resolve()
        ):
            empty_secondary = (
                self._workspace(job_id)
                / f"reference.secondary.empty.{source_language}.srt"
            )
            empty_secondary.write_text("", encoding="utf-8")
            secondary = str(empty_secondary)
            artifacts["empty_secondary_source"] = secondary
            artifacts[f"empty_secondary_{source_language}"] = secondary
            if source_language == "en":
                artifacts["empty_secondary_en"] = secondary
        return {
            "inputs": _source_inputs(primary, secondary, source_language),
            "artifacts": artifacts,
            "source_selection": {
                "primary_text": reference_source,
                "primary_timeline": (
                    "whisper"
                    if reference_source == "youtube"
                    and artifacts.get("youtube_aligned_source")
                    else reference_source
                ),
                "secondary_evidence": (
                    "youtube"
                    if reference_source == "whisper" and youtube_source
                    else "whisper"
                    if reference_source != "whisper"
                    and artifacts.get("whisper_source")
                    else "none"
                ),
                "reason": "自动选择当前最高质量的同语言来源",
            },
        }

    def continue_with_automatic_source(self, job_id: str) -> dict:
        """Let a legacy waiting_capcut job use its downloaded source."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] != "waiting_capcut":
                raise ValueError("只有历史待剪映任务可以直接使用自动字幕继续")
            source_language = normalize_language_pair(
                job.get("options") or {}
            )["source_language"]
            artifacts = job.get("artifacts") or {}
            reference_source = str(job.get("reference_source") or "youtube")
            ready = self._automatic_source_state(
                job_id, job, artifacts, reference_source, source_language
            )
            job.update(
                status="ready_for_translation",
                stage="Sources ready",
                **ready,
            )
            job["updated_at"] = now()
            self._append_event(job, "info", "已使用下载的自动字幕继续")
            self._save(job)
            return job

    def _run_download(self, job_id: str, api_key: str, generation: int = 0) -> None:
        private_cookie_path: Path | None = None
        try:
            if not self._is_current(job_id, generation):
                return
            job = self._update_if_current(
                job_id, generation, status="running", stage="Downloading YouTube assets"
            )
            if job is None:
                return
            from web.downloads import run_download
            options = job["options"]
            source_language = normalize_language_pair(options)["source_language"]
            reference_strategy = options.get("reference_strategy", "adaptive")
            download_config = self._download_stage_config(job)
            artifacts = self._reuse_stage_checkpoint(
                job_id, "download", download_config,
            )
            if artifacts is not None:
                self._event_if_current(
                    job_id, generation, "info", "复用已校验的下载阶段产物",
                )
            else:
                youtube_auth = str(options.get("youtube_auth", "none")).lower()
                if youtube_auth == "file":
                    private_cookie_path = self.private_cookie_store.materialize(
                        job_id, generation
                    )
                cookies_file = str(private_cookie_path) if private_cookie_path else ""
                cookies_from_browser = (
                    youtube_auth
                    if youtube_auth == "chrome"
                    else ""
                )
                if cookies_file or cookies_from_browser:
                    self._event_if_current(
                        job_id,
                        generation,
                        "info",
                        "已启用 YouTube 登录 Cookie",
                    )

                def cancel_check():
                    try:
                        return not self._is_current(job_id, generation)
                    except FileNotFoundError:
                        return True

                artifacts = run_download(
                    job["url"], str(Path(job["workspace"])),
                    proxy=options.get("proxy", ""),
                    download_video=options.get("download_video", True),
                    # The selected reference controls which source is primary, not
                    # whether independent YouTube evidence should be collected.
                    download_subtitles=options.get("download_subtitles", True),
                    download_thumbnail=options.get("download_thumbnail", True),
                    video_quality=options.get("video_quality", "1080"),
                    allow_missing_subtitles=(
                        reference_strategy != "youtube"
                    ),
                    cookies_file=cookies_file,
                    cookies_from_browser=cookies_from_browser,
                    source_language=source_language,
                    emit=lambda message: self._event_if_current(
                        job_id, generation, "info", message
                    ),
                    cancel_check=cancel_check,
                )
                self._record_stage_checkpoint(
                    job_id, generation, "download", download_config, artifacts,
                )
            strategy = reference_strategy
            youtube_source = artifacts.get("youtube_source") or artifacts.get(
                f"youtube_{source_language}", ""
            )
            youtube_quality = _assess_source(
                youtube_source, source_language,
            )
            use_local_reference, whisper_required = _whisper_reference_plan(
                strategy, youtube_quality
            )
            if strategy == "youtube" and not youtube_quality.usable:
                raise RuntimeError(
                    f"YouTube {_language_label(source_language)}字幕缺失或质量不足；"
                    "请选择自适应或本地 Whisper"
                )
            reference_source = "youtube"
            reference_quality = youtube_quality.to_dict()
            source_qualities = {"youtube": youtube_quality.to_dict()}
            if use_local_reference:
                video_path = artifacts.get("video")
                if not video_path:
                    raise RuntimeError(
                        "Adaptive fallback needs the downloaded video; enable video download"
                    )
                whisper_output = (
                    Path(job["workspace"])
                    / f"reference.whisper.{source_language}.srt"
                )
                self._event_if_current(
                    job_id,
                    generation,
                    "info",
                    (
                        f"YouTube {_language_label(source_language)}源存在滚动残句，正在生成独立 Whisper "
                        "语音时间证据"
                        if (
                            strategy == "adaptive"
                            and getattr(
                                youtube_quality, "needs_secondary", False
                            )
                        )
                        else
                        f"YouTube {_language_label(source_language)}质量不足，"
                        "正在生成本地 Whisper 参考"
                    ),
                )
                from pipeline.long_video import _run_cancellable
                asr_plan = asr_runtime_plan(
                    source_language,
                    quality_priority=bool(options.get("asr_quality_priority", False)),
                )
                self._event_if_current(
                    job_id, generation, "info",
                    f"Whisper model={asr_plan['model']} language={source_language} "
                    f"VAD={'on' if asr_plan['use_vad'] else 'off'}; {asr_plan['reason']}",
                )
                command = [
                    sys.executable,
                    str(ROOT / "pipeline" / "tools" / "whisper_transcribe.py"),
                    video_path,
                    "-o",
                    str(whisper_output),
                    "--model",
                    asr_plan["model"],
                    "--language",
                    source_language,
                    "--beam-size",
                    "3",
                ]
                if not asr_plan["use_vad"]:
                    command.append("--no-vad")
                returncode = _run_cancellable(
                    command,
                    cancel_check,
                    emit=lambda message: self._event_if_current(
                        job_id, generation, "info", message
                    ),
                )
                whisper_quality = _assess_source(whisper_output, source_language)
                # 清理临时文件失败（Windows 句柄占用）会让 returncode 非 0，
                # 但 SRT 已生成且质量合格时不应判定失败（误杀可用参考）。
                srt_usable = whisper_quality.usable and whisper_output.exists()
                if (returncode and not srt_usable) or not whisper_quality.usable:
                    if whisper_required:
                        raise RuntimeError(
                            f"本地 Whisper 未生成可用{_language_label(source_language)}参考"
                        )
                    self._event_if_current(
                        job_id,
                        generation,
                        "warning",
                        "补充 Whisper 证据生成失败，已保留可用的 YouTube "
                        "字幕继续处理",
                    )
                else:
                    artifacts["whisper_source"] = str(whisper_output)
                    artifacts[f"whisper_{source_language}"] = str(whisper_output)
                    if source_language == "en":
                        artifacts["whisper_en"] = str(whisper_output)
                    source_qualities["whisper"] = whisper_quality.to_dict()
                    if (
                        youtube_source
                        and getattr(
                            youtube_quality, "needs_timing_alignment", False
                        )
                    ):
                        from pipeline.parser.srt_parser import parse_srt, save_srt
                        from pipeline.timing_alignment import retime_subtitles

                        youtube_source = self._safe_job_path(
                            job_id, youtube_source
                        )
                        aligned_output = (
                            Path(job["workspace"])
                            / f"youtube.aligned.{source_language}.srt"
                        )
                        youtube_subtitles = parse_srt(str(youtube_source))
                        aligned_subtitles = retime_subtitles(
                            youtube_subtitles,
                            parse_srt(str(whisper_output)),
                        )
                        changed_count = sum(
                            original.start != aligned.start
                            or original.end != aligned.end
                            for original, aligned in zip(
                                youtube_subtitles, aligned_subtitles
                            )
                        )
                        if changed_count >= max(
                            1, round(len(youtube_subtitles) * 0.2)
                        ):
                            save_srt(str(aligned_output), aligned_subtitles)
                            artifacts["youtube_aligned_source"] = str(aligned_output)
                            artifacts[f"youtube_{source_language}_aligned"] = str(aligned_output)
                            if source_language == "en":
                                artifacts["youtube_en_aligned"] = str(aligned_output)
                            self._event_if_current(
                                job_id,
                                generation,
                                "info",
                                f"已用 Whisper 校准 {changed_count} 条 "
                                "YouTube 字幕时间，文本保持不变",
                            )
                    if (
                        whisper_required
                        or int(getattr(whisper_quality, "score", 0))
                        >= int(getattr(youtube_quality, "score", 0)) + 5
                    ):
                        reference_source = "whisper"
                        reference_quality = whisper_quality.to_dict()
            artifacts = {
                name: str(self._safe_job_path(job_id, path))
                for name, path in artifacts.items()
            }
            metadata = {}
            metadata_path = artifacts.get("metadata_json")
            if metadata_path:
                try:
                    info = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
                    metadata = {
                        "title": str(info.get("title") or "")[:300],
                        "channel": str(info.get("channel") or info.get("uploader") or "")[:200],
                        "duration": int(info.get("duration") or 0),
                        "thumbnail_url": str(info.get("thumbnail") or "")[:2048],
                    }
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    LOG.warning("Could not read YouTube metadata for %s", job_id)
            if not self._is_current(job_id, generation):
                return
            ready = self._automatic_source_state(
                job_id,
                job,
                artifacts,
                reference_source,
                source_language,
                str(youtube_source or ""),
            )
            job = self._update_if_current(
                job_id,
                generation,
                status="ready_for_translation",
                stage="Sources ready",
                reference_source=reference_source,
                reference_quality=reference_quality,
                source_qualities=source_qualities,
                metadata=metadata,
                **ready,
            )
            if job is None:
                return
            self._event_if_current(
                job_id,
                generation,
                "info",
                "自动字幕素材已就绪，可直接开始翻译",
            )
        except CookieCredentialUnavailable as error:
            if not self._is_current(job_id, generation):
                return
            fields = public_error_fields("youtube_auth_required")
            self._event_if_current(
                job_id, generation, "error", str(fields["message"]),
            )
            failed = self._update_if_current(
                job_id,
                generation,
                status="failed",
                stage="Download failed",
                error=fields["message"],
                error_code=fields["error_code"],
                upstream_status=fields["upstream_status"],
            )
            if failed is not None:
                self._forget_secret(job_id, generation)
        except Exception as error:
            if not self._is_current(job_id, generation):
                return
            LOG.warning(
                "download job %s failed (%s): %s",
                job_id, type(error).__name__,
                safe_public_text(str(error)),
            )
            source_unreliable = any(marker in str(error) for marker in (
                "没有获得可用", "质量不足", "未生成可用", "源语言不一致",
            ))
            fields = public_error_fields(
                "source_unreliable" if source_unreliable else "download_error"
            )
            self._event_if_current(
                job_id, generation, "error", str(fields["message"]),
            )
            failed = self._update_if_current(
                job_id, generation, status="failed", stage="Download failed",
                error=fields["message"],
                error_code=fields["error_code"],
                upstream_status=fields["upstream_status"],
                delivery_status=("source_unreliable" if source_unreliable else None),
            )
            if failed is not None:
                self._forget_secret(job_id, generation)
        finally:
            self.private_cookie_store.cleanup_materialized(private_cookie_path)
            self.private_cookie_store.discard(job_id, generation)

    def attach_capcut(
        self, job_id: str, path: Path,
        language_pair: dict[str, str] | None = None,
    ) -> dict:
        from pipeline.parser.srt_parser import parse_srt
        subtitles = parse_srt(str(path))
        if not subtitles:
            raise ValueError("剪映 SRT 没有有效字幕")
        with self.lock:
            job = self._load(job_id)
            current_pair = normalize_language_pair(job.get("options") or {})
            capcut_quality = _assess_source(path, current_pair["source_language"])
            if capcut_quality.language_ratio < 0.45:
                raise ValueError(
                    f"剪映字幕与任务源语言不一致：任务要求"
                    f"{_language_label(current_pair['source_language'])} SRT"
                )
            if language_pair is not None and language_pair != current_pair:
                raise ValueError("上传字幕的语言必须与任务语言一致")
            # P2: 只下字幕的 URL 任务完成/失败后 也能补第二源证据，回 ready 待重翻。
            if job["status"] not in {
                "waiting_capcut", "ready_for_translation", "failed",
                "completed",
            }:
                raise ValueError("当前任务还不能上传剪映字幕")
            self._ensure_quota(incoming_bytes=path.stat().st_size)
            destination = self._workspace(job_id) / (
                f"capcut.{current_pair['source_language']}.srt"
            )
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            try:
                shutil.copyfile(path, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            existing_secondary = _source_input(
                job, "secondary_srt", "whisper_en",
            )
            if not existing_secondary:
                artifacts = job.get("artifacts") or {}
                existing_secondary = str(
                    artifacts.get("whisper_source")
                    or artifacts.get("youtube_source")
                    or artifacts.get("whisper_en")
                    or artifacts.get("youtube_en")
                    or ""
                )
            if not existing_secondary:
                raise ValueError("任务缺少辅助字幕来源")
            current_primary = _source_input(job, "primary_srt", "capcut_en")
            qualities = job.setdefault("source_qualities", {})
            qualities["capcut"] = capcut_quality.to_dict()
            current_name = str(
                (job.get("source_selection") or {}).get("primary_text")
                or job.get("reference_source") or "youtube"
            )
            current_score = int((qualities.get(current_name) or {}).get("score", 0))
            multilingual = current_pair["source_language"] in {"ja", "ko"}
            if multilingual and current_primary and capcut_quality.score <= current_score:
                primary, secondary = current_primary, (
                    str(destination) if capcut_quality.usable else existing_secondary
                )
                selection_reason = (
                    f"保留 {current_name} 主文本；剪映评分 "
                    f"{capcut_quality.score} 未超过 {current_score}"
                )
            else:
                primary, secondary = str(destination), current_primary or existing_secondary
                selection_reason = (
                    f"剪映评分 {capcut_quality.score} 高于当前来源 {current_score}"
                    if multilingual else "英语兼容流程使用剪映主时间轴"
                )
                current_name = "capcut"
            job["inputs"] = _source_inputs(
                primary, secondary, current_pair["source_language"],
            )
            job["source_selection"] = {
                "primary_text": current_name,
                "primary_timeline": current_name,
                "secondary_evidence": "capcut" if current_name != "capcut" else "automatic",
                "reason": selection_reason,
            }
            job["status"] = "ready_for_translation"
            job["stage"] = f"CapCut uploaded ({len(subtitles)} subtitles)"
            job.setdefault("artifacts", {})[destination.name] = str(destination)
            job["updated_at"] = now()
            self._append_event(
                job, "info",
                f"剪映{_language_label(current_pair['source_language'])}字幕已上传: "
                f"{len(subtitles)} 条；{selection_reason}",
            )
            self._save(job)
            return job

    @staticmethod
    def _normalize_translation_options(
            current: dict[str, Any], updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = {
            **DEFAULT_TRANSLATION_OPTIONS,
            **current,
            **(updates or {}),
        }
        options["model"] = str(options.get("model", "")).strip()
        options["round2_model"] = str(
            options.get("round2_model", "")
        ).strip() or options["model"]
        options["proxy"] = str(options.get("proxy", "")).strip()
        options["game"] = str(options.get("game", "")).strip()
        options.update(normalize_language_pair(options))
        try:
            options["batch_size"] = int(options.get("batch_size", 28))
        except (TypeError, ValueError) as error:
            raise ValueError("批次大小必须是整数") from error
        options["local_whisper"] = bool(options.get("local_whisper"))
        options["dry_run"] = bool(options.get("dry_run"))
        options["burn_after_translation"] = bool(
            options.get("burn_after_translation")
        )
        try:
            options["base_url"] = normalize_llm_base_url(
                options.get("base_url"), required=not options["dry_run"],
            )
        except ValueError as error:
            raise ValueError(str(error)) from None
        if not options["model"]:
            raise ValueError("请输入模型名称")
        if not options["game"]:
            raise ValueError("请选择游戏")
        if not 1 <= options["batch_size"] <= 100:
            raise ValueError("批次大小必须在 1 到 100 之间")
        return options

    def start_translation(self, job_id: str, secret: str, resume: bool = False,
                          translation_options: dict[str, Any] | None = None) -> dict:
        with self.lock:
            self._require_accepting_work()
            job = self._load(job_id)
            source_language = normalize_language_pair(
                job.get("options") or {}
            )["source_language"]
            primary = self._resolve_stored_input(
                job_id, _source_input(job, "primary_srt", "capcut_en"))
            secondary = self._resolve_stored_input(
                job_id, _source_input(job, "secondary_srt", "whisper_en"))
            if not primary:
                raise ValueError(
                    f"任务缺少可用的{_language_label(source_language)}主字幕来源"
                )
            artifacts = job.get("artifacts") or {}
            if not secondary:
                evidence = (
                    artifacts.get("whisper_source")
                    or artifacts.get("whisper_en")
                    or artifacts.get("youtube_source")
                    or artifacts.get("youtube_en")
                )
                if evidence:
                    secondary = self._resolve_stored_input(job_id, evidence)
                    self._append_event(
                        job, "info",
                        f"正在使用同语言的自适应 {_language_label(source_language)}参考",
                    )
            if not secondary:
                raise ValueError(
                    f"请提供 Whisper 或 YouTube {_language_label(source_language)}辅助字幕"
                )
            job["inputs"] = _source_inputs(primary, secondary, source_language)
            if job["status"] not in {"ready_for_translation", "failed"}:
                raise ValueError("当前任务状态不允许开始翻译")

            options = self._normalize_translation_options(
                job.get("options", {}), translation_options
            )

            if options["local_whisper"] and not job["inputs"].get("video"):
                downloaded_video = job.get("artifacts", {}).get("video")
                if downloaded_video:
                    job["inputs"]["video"] = str(
                        self._safe_job_path(job_id, downloaded_video)
                    )
                else:
                    raise ValueError("局部 Whisper 复核需要已上传或已下载的视频")
            if secret:
                self.secrets[job_id] = secret
            api_key = self.secrets.get(job_id, "")
            if not options["dry_run"] and not api_key:
                raise ValueError("页面刷新后请重新输入 API key 再开始翻译")
            generation = job.get("generation", 0) + 1
            options["api_key_configured"] = bool(api_key)
            job.update(
                status="queued", stage="Translation queued", error=None,
                error_code=None, generation=generation, resume=bool(resume),
                options=options, progress={"completed": 0, "total": 0},
                auto_resume=None,
            )
            job["updated_at"] = now()
            self._save(job)
            self._submit(job_id, self._run, job_id, api_key, generation)
        return self.get(job_id)

    def _update(self, job_id: str, **changes: Any) -> dict:
        with self.lock:
            job = self._load(job_id)
            job.update(changes)
            job["updated_at"] = now()
            self._save(job)
            return job

    @staticmethod
    def _verify_snapshot_integrity(snapshot_dir: Path, recorded: dict) -> None:
        """Fail fast on a corrupted config snapshot before translation starts.

        2026-09-06 实测（任务 6311f11a3cad）：organize 的 Windows 句柄冲突曾把
        config/<generation>/ 目录移动到一半（alias.json 等被搬走删除），之后每次
        resume 都在流水线深处 FileNotFoundError，用户只看到含糊的"翻译任务失败"。
        启动前逐文件核对建快照时记录的 sha256（glossary.db 打开时会幂等重写
        字节、TM/术语文件才是行为参数——只验证存在），损坏立即给出可行动错误。
        """
        recorded_hashes = recorded.get("sha256") or {}
        missing = []
        mismatched = []
        for name in sorted(recorded_hashes):
            candidate = snapshot_dir / name
            if not candidate.is_file():
                missing.append(name)
                continue
            if name == "glossary.db":
                continue
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest != recorded_hashes[name]:
                mismatched.append(name)
        if missing or mismatched:
            raise FileNotFoundError(
                "任务配置快照已损坏"
                + (f"（缺失: {', '.join(missing[:5])}）" if missing else "")
                + (
                    f"（内容校验失败: {', '.join(mismatched[:5])}）"
                    if mismatched else ""
                )
                + "；请使用“从头重跑”创建新快照"
            )

    def _ensure_config_snapshot(self, job_id: str, generation: int) -> str:
        with self.lock:
            job = self._load(job_id)
            if not self._is_current_job(job, generation):
                raise PipelineCancelled("Task cancelled")
            existing = job.get("config_snapshot", {})
            existing_path = (
                self._safe_job_path(job_id, existing["path"])
                if existing.get("path") else None
            )
            if existing_path is not None and existing_path.is_dir():
                self._verify_snapshot_integrity(existing_path, existing)
                return str(existing_path)
            if existing:
                raise FileNotFoundError(
                    "任务配置快照已丢失；请使用“从头重跑”创建新快照"
                )

            snapshot = (
                self._workspace(job_id) / "config"
                / f"generation-{generation}-{uuid.uuid4().hex[:6]}"
            )
            snapshot.mkdir(parents=True, exist_ok=False)
            source_dir = ROOT / "data"
            files = [
                "alias.json", "glossary.db", "music.txt",
                "prompt_template.txt", "tricky_terms.json",
                "ja_person_terms.json", "ja_place_terms.json",
                "ko_person_terms.json",
            ]
            optional_files = ["translation_memory.json"]
            try:
                for name in files:
                    source = source_dir / name
                    destination = snapshot / name
                    if name == "glossary.db":
                        source_db = sqlite3.connect(source)
                        target_db = sqlite3.connect(destination)
                        try:
                            source_db.backup(target_db)
                        finally:
                            target_db.close()
                            source_db.close()
                    else:
                        shutil.copy2(source, destination)
                for source in sorted(source_dir.glob("asr_corrections*.json")):
                    if source.is_file():
                        shutil.copy2(source, snapshot / source.name)
                        files.append(source.name)
                # Prompt selection is language-pair specific.  Snapshot the
                # whole directory so a later web edit cannot silently change
                # a running/resumed Japanese job.
                prompts_source = source_dir / "prompts"
                if prompts_source.is_dir():
                    shutil.copytree(prompts_source, snapshot / "prompts")
                for name in optional_files:
                    source = source_dir / name
                    if source.is_file():
                        try:
                            payload = json.loads(source.read_text(encoding="utf-8"))
                            from pipeline.translation_memory import SCHEMA_VERSION

                            memory_schema = payload.get(
                                "schema_version", SCHEMA_VERSION,
                            )
                            if memory_schema not in {1, SCHEMA_VERSION}:
                                raise ValueError(
                                    "translation memory schema version 不兼容"
                                )
                            approved = [
                                entry for entry in payload.get("entries", [])
                                if isinstance(entry, dict) and entry.get("approved")
                            ]
                            atomic_json(snapshot / name, {
                                "schema_version": memory_schema,
                                "kind": "translation-memory",
                                "entries": approved,
                            })
                            files.append(name)
                        except (OSError, ValueError, TypeError, json.JSONDecodeError):
                            LOG.warning(
                                "Translation memory snapshot skipped for %s",
                                job_id,
                            )
                hashes = {}
                for name in files:
                    hashes[name] = hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
                prompt_snapshot = snapshot / "prompts"
                if prompt_snapshot.is_dir():
                    for prompt_file in sorted(prompt_snapshot.rglob("*")):
                        if prompt_file.is_file():
                            relative = prompt_file.relative_to(snapshot).as_posix()
                            hashes[relative] = hashlib.sha256(
                                prompt_file.read_bytes()
                            ).hexdigest()
            except Exception:
                shutil.rmtree(snapshot, ignore_errors=True)
                raise

            job["config_snapshot"] = {"path": str(snapshot), "sha256": hashes}
            job["updated_at"] = now()
            self._save(job)
            return str(snapshot)

    def _collect_available_metrics(self, job_id: str, job: dict | None = None) -> dict:
        """Read completed or partial stage metrics without mutating manifests."""
        job = job or self.get(job_id)
        output_dir = self._workspace(job_id)
        metrics_path = output_dir / "final.zh.pipeline.metrics.json"
        metrics = {}
        if metrics_path.exists():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                metrics = {}

        from pipeline.pipeline_manifest import load_manifest, summarize_manifest_metrics
        for stage, filename in (
            ("round1", "final.round1.zh.srt.manifest.json"),
            ("round2", "final.round2.zh.srt.manifest.json"),
        ):
            manifest_path = output_dir / filename
            if stage in metrics or not manifest_path.exists():
                continue
            try:
                manifest = load_manifest(str(manifest_path))
                metrics[stage] = (
                    manifest.get("metrics")
                    or summarize_manifest_metrics(manifest)
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        metrics["sources"] = job.get("source_qualities", {})
        metrics["selected_reference"] = job.get("reference_source", "")
        options = job.get("options") or {}
        stage_models = {
            "round1": options.get("model", ""),
            "round2": options.get("round2_model") or options.get("model", ""),
        }
        for stage, model in stage_models.items():
            stage_metrics = metrics.get(stage)
            if not isinstance(stage_metrics, dict):
                continue
            stage_metrics["model"] = str(model or "")
            stage_metrics["pricing"] = estimate_model_cost(
                str(model or ""),
                stage_metrics.get("tokens_in", 0),
                stage_metrics.get("tokens_out", 0),
            )
        return metrics

    def _run(self, job_id: str, api_key: str, generation: int = 0) -> None:
        """P0-2：_run_attempt 的有界自动续跑包装。

        仅对可重试 provider 失败（provider_unavailable / api_error /
        invalid_provider_response）做 ≤Config.LLM_AUTO_RESUME_MAX 次指数退避
        自动续跑；欠费/鉴权/配置类永不续跑；等待可被 generation 变更（取消/
        暂停/重置）打断；续跑路径不清 secret，只有终态失败才清。不触碰
        _delivery_status 与任何质量门禁；成功路径行为与现状完全一致。
        """
        retryable_codes = {
            "provider_unavailable", "api_error", "invalid_provider_response",
        }
        attempt = 0
        history: list[dict] = []
        while True:
            try:
                return self._run_attempt(job_id, api_key, generation)
            except PipelineCancelled:
                return
            except LLMAPIError as error:
                if not self._is_current(job_id, generation):
                    return
                attempt += 1
                fields = error.public_fields()
                code = str(fields["error_code"])
                history.append({
                    "attempt": attempt,
                    "error_code": code,
                    "upstream_status": fields["upstream_status"],
                })
                if (
                    code not in retryable_codes
                    or attempt > Config.LLM_AUTO_RESUME_MAX
                    or not self._is_current(job_id, generation)
                ):
                    self._fail_provider_error(
                        job_id, generation, error,
                        auto_resume={
                            "attempts": attempt,
                            "last_error_code": code,
                            "history": history,
                        },
                    )
                    return
                delay = int(
                    Config.LLM_AUTO_RESUME_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                )
                history[-1]["next_delay_seconds"] = delay
                self._queue_auto_resume(
                    job_id, generation, attempt, code, delay, history,
                )
                if not self._wait_interruptible(job_id, generation, delay):
                    return

    def _fail_provider_error(
            self, job_id: str, generation: int, error: LLMAPIError,
            *, auto_resume: dict | None = None,
    ) -> None:
        """终态 provider 失败落盘（复刻原 _run 的 except LLMAPIError 行为）。"""
        fields = error.public_fields()
        LOG.warning(
            "job %s stopped by provider error code=%s status=%s",
            job_id, fields["error_code"], fields["upstream_status"],
        )
        self._event_if_current(
            job_id, generation, "error", str(fields["message"]),
        )
        stage = (
            "API authentication failed"
            if error.code == "authentication_error"
            else "API request failed"
        )
        changes: dict[str, Any] = {
            "status": "failed", "stage": stage,
            "error": fields["message"], "error_code": fields["error_code"],
            "upstream_status": fields["upstream_status"],
            "metrics": self._collect_available_metrics(job_id),
        }
        if auto_resume is not None:
            changes["auto_resume"] = auto_resume
        failed = self._update_if_current(job_id, generation, **changes)
        if failed is not None:
            self._forget_secret(job_id, generation)

    def _queue_auto_resume(
            self, job_id: str, generation: int, attempt: int,
            code: str, delay: int, history: list[dict],
    ) -> None:
        """把任务置回 queued 并记录续跑摘要；secret 保留供续跑复用。"""
        self._update_if_current(
            job_id, generation, status="queued",
            stage=(
                f"自动重试 {attempt}/{Config.LLM_AUTO_RESUME_MAX}，{delay}s 后继续"
            ),
            resume=True, error=None, error_code=None,
            auto_resume={
                "attempts": attempt, "last_error_code": code,
                "next_delay_seconds": delay, "history": list(history),
            },
        )
        self._event_if_current(
            job_id, generation, "info",
            f"上游暂时不可用（{code}），{delay} 秒后自动续跑（第 {attempt} 次）；"
            "已完成批次会复用，无需手动操作",
        )

    def _wait_interruptible(
            self, job_id: str, generation: int, delay: int,
    ) -> bool:
        """睡 delay 秒（每秒查 generation）；被取消/暂停/重置则立即返回 False。"""
        waited = 0
        while waited < delay:
            time.sleep(1)
            waited += 1
            if not self._is_current(job_id, generation):
                return False
        return self._is_current(job_id, generation)


    def _run_attempt(self, job_id: str, api_key: str, generation: int = 0) -> None:
        log_handler = self._pipeline_log_handler(job_id, generation)
        logging.getLogger().addHandler(log_handler)
        pipeline_loggers = [
            logging.getLogger("main"), logging.getLogger("long_video"),
            logging.getLogger("httpx"),
        ]
        previous_levels = [logger.level for logger in pipeline_loggers]
        for logger in pipeline_loggers:
            logger.setLevel(logging.INFO)
        try:
            if not self._is_current(job_id, generation):
                return
            source_language = normalize_language_pair(
                (self.get(job_id).get("options") or {})
            )["source_language"]
            job = self._update_if_current(
                job_id, generation, status="running",
                stage=f"Preparing {_language_label(source_language)} sources",
            )
            if job is None:
                return
            self._event_if_current(
                job_id, generation, "info",
                f"Building {_language_label(source_language)} source union",
            )
            from pipeline.long_video import run_long_video
            workspace = self._workspace(job_id)
            output = workspace / "final.zh.srt"
            options = job["options"]
            def cancel_check():
                return not self._is_current(job_id, generation)

            def progress_callback(completed: int, total: int) -> None:
                self._update_if_current(
                    job_id, generation,
                    stage=f"Translating batch {completed}/{total}",
                    progress={"completed": completed, "total": total},
                )

            data_dir = self._ensure_config_snapshot(job_id, generation)

            status = run_long_video(
                capcut_en=str(self._safe_job_path(
                    job_id, _source_input(job, "primary_srt", "capcut_en"),
                )),
                whisper_en=str(self._safe_job_path(
                    job_id, _source_input(job, "secondary_srt", "whisper_en"),
                )),
                output=str(output), model=options["model"],
                round2_model=options.get("round2_model") or options["model"],
                api_key=api_key, batch_size=int(options["batch_size"]), game=options["game"],
                resume=bool(job.get("resume")),
                video=(str(self._safe_job_path(job_id, job["inputs"]["video"]))
                       if job["inputs"].get("video") else None),
                local_whisper=bool(options.get("local_whisper")),
                dry_run=bool(options.get("dry_run")),
                base_url=options.get("base_url", ""), cancel_check=cancel_check,
                proxy=options.get("proxy", ""),
                progress_callback=progress_callback,
                data_dir=data_dir,
                video_context={
                    "title": str((job.get("metadata") or {}).get("title", "")),
                    "channel": str((job.get("metadata") or {}).get("channel", "")),
                },
                source_language=options["source_language"],
                target_language=options["target_language"],
            )
            if not self._is_current(job_id, generation):
                return
            if status:
                raise RuntimeError(self._pipeline_failure_message(output, status))
            if not self._event_if_current(
                job_id, generation, "info", "Pipeline completed"
            ):
                return
            current = self.get(job_id)
            artifacts = dict(current.get("artifacts", {}))
            for path in workspace.glob("*"):
                if path.is_file() and not path.is_symlink():
                    artifacts[path.name] = str(path)
            for suffix in (".manifest.json", ".risk-queue.json"):
                path = Path(str(output) + suffix)
                if path.exists():
                    artifacts[path.name] = str(path)
            metrics = self._collect_available_metrics(job_id, current)
            delivery_status = _delivery_status(
                metrics, dry_run=bool(options.get("dry_run")),
            )
            current["artifacts"] = artifacts
            organization = self._organize_technical_files(current)
            organized_job = organization["job"]
            completed = self._update_if_current(
                job_id, generation, status="completed",
                stage={
                    "deliverable": "Deliverable",
                    "review_required": "Human review required",
                    "source_unreliable": "Source quality requires review",
                    "automatic_complete": "Automatic translation complete",
                }[delivery_status],
                delivery_status=delivery_status,
                artifacts=organized_job["artifacts"], metrics=metrics,
                config_snapshot=organized_job.get("config_snapshot"),
                technical_files_folder=organized_job.get("technical_files_folder"),
                completed_at=now(),
                error=None, error_code=None,
            )
            if completed is not None:
                if organization["moved"] or organization["moved_directories"]:
                    self._event(
                        job_id,
                        "info",
                        "已将过程文件收进“过程文件”文件夹",
                    )
                self._forget_secret(job_id, generation)
                if options.get("burn_after_translation"):
                    self._defer_or_start_requested_burn(job_id)
        except PipelineCancelled:
            return
        except LLMAPIError:
            # P0-2：交外层 _run 包装循环决定自动续跑或终态失败
            raise
        except Exception as error:
            if not self._is_current(job_id, generation):
                return
            LOG.warning(
                "job %s failed (%s); exception details discarded",
                job_id, type(error).__name__,
            )
            fields = public_error_fields("pipeline_error")
            self._event_if_current(
                job_id, generation, "error", str(fields["message"]),
            )
            failed = self._update_if_current(
                job_id, generation, status="failed", stage="Failed",
                error=fields["message"],
                error_code=fields["error_code"],
                upstream_status=fields["upstream_status"],
                metrics=self._collect_available_metrics(job_id),
            )
            if failed is not None:
                self._forget_secret(job_id, generation)
        finally:
            logging.getLogger().removeHandler(log_handler)
            for logger, level in zip(pipeline_loggers, previous_levels):
                logger.setLevel(level)

    def risks(self, job_id: str) -> list[dict]:
        job = self._load(job_id)
        artifacts = job.get("artifacts", {})
        generated_value = next(
            (path for name, path in artifacts.items()
             if name.endswith(".risk.generated.json")),
            None,
        )
        if generated_value:
            from pipeline.risk_queue_store import (
                load_generated_risks,
                load_review_state,
                load_round2_results,
                merge_risk_views,
            )
            generated = load_generated_risks(
                str(self._safe_job_path(job_id, generated_value))
            )
            results_value = next(
                (path for name, path in artifacts.items()
                 if name.endswith(".round2.results.json")),
                None,
            )
            review_value = next(
                (path for name, path in artifacts.items()
                 if name.endswith(".review-state.json")),
                None,
            )
            results = (
                load_round2_results(
                    str(self._safe_job_path(job_id, results_value))
                ).get("items", [])
                if results_value else []
            )
            review = (
                load_review_state(
                    str(self._safe_job_path(job_id, review_value))
                )
                if review_value else []
            )
            return merge_risk_views(generated, results, review)

        for name, path in job.get("artifacts", {}).items():
            if name.endswith(".risk-queue.json"):
                risk_path = self._safe_job_path(job_id, path)
                payload = json.loads(risk_path.read_text(encoding="utf-8"))
                items = payload.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("风险队列格式错误")
                return items
        return []

    def artifact_path(self, job_id: str, name: str) -> Path:
        job = self.get(job_id)
        value = job.get("artifacts", {}).get(name)
        if not value:
            raise FileNotFoundError(name)
        path = self._safe_job_path(job_id, value)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(name)
        return path

    def generate_round2_suggestion(
        self,
        job_id: str,
        key: str,
        secret: str,
    ) -> list[dict]:
        """Generate one deferred Round 2 comparison without blocking export."""
        if not secret.strip():
            raise ValueError("请输入 API Key 后再生成 Round 2 建议")
        job = self.get(job_id)
        if job.get("status") != "completed":
            raise ValueError("任务完成后才能按需生成 Round 2 建议")
        item = next(
            (entry for entry in self.risks(job_id) if entry.get("key") == key),
            None,
        )
        if item is None:
            raise KeyError(key)

        from pipeline.long_video import (
            _evaluate_round_two_decision,
            _load_glossary,
            _parse_round_two_response,
            _relevant_glossary,
            _round_two_prompt,
        )
        from pipeline.pipeline_manifest import file_sha256
        from pipeline.risk_queue_store import (
            load_round2_results,
            save_round2_results,
        )
        from pipeline.translate.llm import create_translator

        options = self._normalize_translation_options(job.get("options", {}))
        data_dir = (
            str(self._safe_job_path(
                job_id, job.get("config_snapshot", {}).get("path")
            ))
            if job.get("config_snapshot", {}).get("path")
            else str(ROOT / "data")
        )
        glossary = _load_glossary(options["game"], data_dir)
        translator = create_translator(
            api_key=secret,
            model=options.get("round2_model") or options["model"],
            temperature=0.1,
            base_url=options.get("base_url", ""),
            proxy=options.get("proxy", ""),
        )
        prompt = _round_two_prompt(
            [item],
            _relevant_glossary([item], glossary),
        )
        response, tokens_in, tokens_out = translator._call_api(prompt)
        subtitle_id = int(item["subtitle_id"])
        parsed = _parse_round_two_response(response, {subtitle_id})
        decision = parsed[subtitle_id]
        outcome = _evaluate_round_two_decision(item, decision, glossary)

        artifacts = job.get("artifacts", {})
        results_value = next(
            (path for name, path in artifacts.items()
             if name.endswith(".round2.results.json")),
            None,
        )
        generated_value = next(
            (path for name, path in artifacts.items()
             if name.endswith(".risk.generated.json")),
            None,
        )
        if results_value is None or generated_value is None:
            raise FileNotFoundError("Round 2 state is unavailable")
        results_path = self._safe_job_path(job_id, results_value)
        generated_path = self._safe_job_path(job_id, generated_value)

        with self.lock:
            payload = load_round2_results(str(results_path))
            by_key = {
                str(entry.get("key")): dict(entry)
                for entry in payload.get("items", [])
                if entry.get("key")
            }
            result = by_key.setdefault(key, {"key": key})
            result.update({
                "round2_text": decision["text"],
                "decision": decision["decision"],
                "confidence": decision["confidence"],
                "reason": decision["reason"],
                "accepted": outcome["accepted"],
                "applied": outcome["applied"],
                "validation_reasons": outcome["validation_reasons"],
                "state": "resolved" if outcome["accepted"] else "review",
                "on_demand": True,
            })
            metrics = dict(payload.get("metrics") or {})
            metrics["on_demand_attempts"] = int(
                metrics.get("on_demand_attempts", 0)
            ) + 1
            metrics["on_demand_tokens_in"] = int(
                metrics.get("on_demand_tokens_in", 0)
            ) + int(tokens_in)
            metrics["on_demand_tokens_out"] = int(
                metrics.get("on_demand_tokens_out", 0)
            ) + int(tokens_out)
            save_round2_results(
                str(results_path),
                by_key.values(),
                metrics=metrics,
                risk_sha256=file_sha256(str(generated_path)),
            )
            self._event(job_id, "info", f"Round 2 suggestion generated for {key}")
        return self.risks(job_id)

    def update_risk(self, job_id: str, key: str, changes: dict) -> list[dict]:
        if not key or len(key) > 256:
            raise ValueError("Invalid risk key")
        allowed_states = {"pending", "review", "resolved", "verified"}
        state = changes.get("state")
        if state is not None and state not in allowed_states:
            raise ValueError(f"Invalid risk state: {state}")
        remember_for_future = changes.get("remember_for_future", False)
        if not isinstance(remember_for_future, bool):
            raise ValueError("remember_for_future 必须是布尔值")
        memory_error_type = changes.get("memory_error_type", "")
        if not isinstance(memory_error_type, str):
            raise ValueError("memory_error_type 必须是文本")
        memory_error_type = memory_error_type.strip()[:80]
        with self.lock:
            job = self._load(job_id)
            artifacts = job.get("artifacts", {})
            generated_value = next(
                (path for name, path in artifacts.items()
                 if name.endswith(".risk.generated.json")),
                None,
            )
            if generated_value is not None:
                current_items = self.risks(job_id)
                if not any(item.get("key") == key for item in current_items):
                    raise KeyError(key)
                updates = {
                    field: value for field, value in changes.items()
                    if field in {"state", "round2_text", "note"}
                }
                for field in ("round2_text", "note"):
                    if field in updates:
                        if not isinstance(updates[field], str):
                            raise ValueError(f"{field} 必须是文本")
                        updates[field] = updates[field].strip()[:10000]

                from pipeline.risk_queue_store import (
                    ensure_review_state,
                    update_review_state,
                )
                review_value = next(
                    (path for name, path in artifacts.items()
                     if name.endswith(".review-state.json")),
                    None,
                )
                review_path = (
                    self._safe_job_path(job_id, review_value)
                    if review_value else
                    self._workspace(job_id) / "final.zh.review-state.json"
                )
                ensure_review_state(str(review_path))
                review_updates = {}
                if "state" in updates:
                    review_updates["state"] = updates["state"]
                if "round2_text" in updates:
                    review_updates["text"] = updates["round2_text"]
                if "note" in updates:
                    review_updates["note"] = updates["note"]
                update_review_state(str(review_path), key, **review_updates)
                current_item = next(
                    item for item in current_items if item.get("key") == key
                )
                self._remember_review_edit(
                    job,
                    current_item,
                    review_updates.get("text", ""),
                    error_type=memory_error_type,
                    approve=remember_for_future,
                )
                job.setdefault("artifacts", {})[review_path.name] = str(review_path)
                self._save(job)
                merged = self.risks(job_id)
                self._write_reviewed_srt(job, merged)
                self._event(job_id, "info", f"Risk item {key} updated")
                return merged

            risk_value = next((path for name, path in job.get("artifacts", {}).items()
                               if name.endswith(".risk-queue.json")), None)
            if risk_value is None:
                raise FileNotFoundError("Risk queue is not available yet")
            risk_path = self._safe_job_path(job_id, risk_value)
            payload = json.loads(risk_path.read_text(encoding="utf-8"))
            if not isinstance(payload.get("items"), list):
                raise ValueError("风险队列格式错误")
            for item in payload["items"]:
                if item.get("key") != key:
                    continue
                updates = {field: value for field, value in changes.items()
                           if field in {"state", "round2_text", "note"}}
                for field in ("round2_text", "note"):
                    if field in updates:
                        if not isinstance(updates[field], str):
                            raise ValueError(f"{field} 必须是文本")
                        updates[field] = updates[field].strip()[:10000]
                item.update(updates)
                atomic_json(risk_path, payload)
                self._remember_review_edit(
                    job,
                    item,
                    updates.get("round2_text", ""),
                    error_type=memory_error_type,
                    approve=remember_for_future,
                )
                self._write_reviewed_srt(job, payload["items"])
                self._event(job_id, "info", f"Risk item {key} updated")
                return payload["items"]
        raise KeyError(key)

    def bulk_update_risks(
        self, job_id: str, changes: list[dict],
    ) -> list[dict]:
        """Persist a batch of human decisions and rebuild the SRT once."""
        if not isinstance(changes, list) or not 1 <= len(changes) <= 1000:
            raise ValueError("批量审校必须包含 1–1000 条修改")
        allowed_states = {"pending", "review", "resolved", "verified"}
        normalized = []
        seen = set()
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("批量审校条目格式错误")
            key = str(change.get("key", "")).strip()
            state = change.get("state")
            text = change.get("text", "")
            if not key or len(key) > 256 or key in seen:
                raise ValueError("批量审校包含无效或重复的风险 key")
            if state not in allowed_states:
                raise ValueError(f"Invalid risk state: {state}")
            if not isinstance(text, str):
                raise ValueError("text 必须是文本")
            seen.add(key)
            normalized.append({
                "key": key,
                "state": state,
                "text": text.strip()[:10000],
            })

        with self.lock:
            job = self._load(job_id)
            current = self.risks(job_id)
            current_keys = {
                str(item.get("key")) for item in current if item.get("key")
            }
            if not seen.issubset(current_keys):
                raise KeyError("Risk item not found")
            artifacts = job.get("artifacts", {})
            generated_value = next(
                (path for name, path in artifacts.items()
                 if name.endswith(".risk.generated.json")),
                None,
            )
            if generated_value is None:
                raise ValueError("旧版风险队列不支持批量审校")

            from pipeline.risk_queue_store import (
                ensure_review_state,
                update_review_states,
            )
            review_value = next(
                (path for name, path in artifacts.items()
                 if name.endswith(".review-state.json")),
                None,
            )
            review_path = (
                self._safe_job_path(job_id, review_value)
                if review_value else
                self._workspace(job_id) / "final.zh.review-state.json"
            )
            ensure_review_state(str(review_path))
            update_review_states(str(review_path), normalized)
            job.setdefault("artifacts", {})[review_path.name] = str(review_path)
            self._save(job)
            merged = self.risks(job_id)
            self._write_reviewed_srt(job, merged)
            self._event(
                job_id, "info",
                f"Bulk reviewed {len(normalized)} risk items",
            )
            return merged

    def _remember_review_edit(
        self,
        job: dict,
        item: dict,
        final_text: str,
        *,
        error_type: str,
        approve: bool,
    ) -> dict | None:
        """Archive a real human correction; only approved entries are reusable."""
        final_text = str(final_text or "").strip()
        previous = str(item.get("translated", "")).strip()
        source = next((
            str(item.get(field, "")).strip()
            for field in (
                "source_text", "primary_evidence", "secondary_evidence",
                "english", "capcut_en", "whisper_en",
            )
            if str(item.get(field, "")).strip()
        ), "")
        if not source or not final_text or final_text == previous:
            return None
        from pipeline.translation_memory import TranslationMemoryStore

        reasons = item.get("reasons") or []
        resolved_type = error_type or (
            str(reasons[0]) if reasons else "其他"
        )
        language_pair = normalize_language_pair(job.get("options") or {})
        return TranslationMemoryStore(TRANSLATION_MEMORY_PATH).record_edit(
            source=source,
            previous=previous,
            final=final_text,
            game=str(job.get("options", {}).get("game", "wuwa")),
            error_type=resolved_type,
            job_id=str(job.get("id", "")),
            risk_key=str(item.get("key", "")),
            approve=approve,
            source_language=language_pair["source_language"],
            target_language=language_pair["target_language"],
        )

    def _write_reviewed_srt(self, job: dict, items: list[dict]) -> None:
        from pipeline.parser.srt_parser import parse_srt, save_srt
        from pipeline.preprocess.dedupe import clean_export_subtitles

        output_dir = self._workspace(job["id"])
        artifacts = job.get("artifacts") or {}
        semantic_value = next(
            (
                value for name, value in artifacts.items()
                if name.endswith("final.round2.zh.srt")
                or name == "final.round2.zh.srt"
            ),
            None,
        )
        source = (
            self._safe_job_path(job["id"], semantic_value)
            if semantic_value else output_dir / "final.zh.srt"
        )
        if not source.is_file():
            raise FileNotFoundError("Final SRT is not available yet")
        subtitles = parse_srt(str(source))
        by_id = {subtitle.id: subtitle for subtitle in subtitles}
        for item in items:
            subtitle = by_id.get(int(item.get("subtitle_id") or 0))
            if subtitle is None:
                continue
            if item.get("state") == "resolved":
                selected = (
                    str(item.get("manual_text", "")).strip()
                    or str(item.get("round2_text", "")).strip()
                )
                if selected:
                    subtitle.text = selected
            elif item.get("state") == "verified" and str(item.get("translated", "")).strip():
                subtitle.text = str(item["translated"]).strip()
        subtitles = clean_export_subtitles(subtitles)
        display_map_value = next(
            (
                value for name, value in artifacts.items()
                if name.endswith(".display-map.json")
            ),
            None,
        )
        if display_map_value:
            from pipeline.display_cues import redistribute_subtitles
            mapping_path = self._safe_job_path(job["id"], display_map_value)
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            subtitles = redistribute_subtitles(subtitles, mapping)
        from pipeline.display_cues import enforce_minimum_duration
        subtitles = enforce_minimum_duration(subtitles)
        from pipeline.postprocess.validate import validate_display_cues
        validate_display_cues(subtitles)
        reviewed = output_dir / "final.reviewed.zh.srt"
        temp = reviewed.with_suffix(reviewed.suffix + ".tmp")
        save_srt(str(temp), subtitles)
        os.replace(temp, reviewed)
        job.setdefault("artifacts", {})[reviewed.name] = str(reviewed)
        job["updated_at"] = now()
        self._save(job)
        if bool((job.get("options") or {}).get("burn_after_translation")):
            self._defer_or_start_requested_burn(job["id"])

    def cancel(self, job_id: str) -> dict:
        """Mark a job as cancelled. Works even on stuck running jobs."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] in ("completed", "failed", "cancelled"):
                raise ValueError("任务已结束，无需取消")
            job["status"] = "cancelled"
            job["stage"] = "Cancelled by user"
            job["generation"] = job.get("generation", 0) + 1
            job["error"] = None
            job["error_code"] = None
            job.setdefault("options", {})["api_key_configured"] = False
            job["updated_at"] = now()
            self._append_event(job, "info", "用户取消了任务")
            self._save(job)
            self.secrets.pop(job_id, None)
            self.private_cookie_store.discard(job_id)
            future = self.futures.get(job_id)
            if future is not None:
                future.cancel()
            return job

    def pause(self, job_id: str) -> dict:
        """Safely stop active work while preserving all resumable artifacts."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] not in {
                "queued", "running", "waiting_capcut", "ready_for_translation",
            }:
                raise ValueError("当前任务无法暂停")
            previous_status = job["status"]
            job.update(
                status="paused",
                stage="Paused by user",
                paused_from=previous_status,
                generation=job.get("generation", 0) + 1,
                error=None,
                error_code=None,
            )
            job["updated_at"] = now()
            self._append_event(job, "info", "用户暂停了任务；已有文件和翻译进度已保留")
            self._save(job)
            self.private_cookie_store.discard(job_id)
            future = self.futures.get(job_id)
            if future is not None:
                future.cancel()
            return job

    def resume(self, job_id: str) -> dict:
        """Resume an idle task or continue a paused pipeline from manifests."""
        with self.lock:
            self._require_accepting_work()
            job = self._load(job_id)
            if job["status"] != "paused":
                raise ValueError("只有已暂停的任务可以继续")
            previous_status = str(job.get("paused_from") or "ready_for_translation")
            job.pop("paused_from", None)

            if previous_status == "waiting_capcut":
                job.update(
                    status="waiting_capcut",
                    stage="Waiting for CapCut English SRT",
                )
                job["updated_at"] = now()
                self._append_event(job, "info", "任务已继续，等待上传剪映英文字幕")
                self._save(job)
                return job

            if previous_status == "ready_for_translation":
                job.update(status="ready_for_translation", stage="Sources ready")
                job["updated_at"] = now()
                self._append_event(job, "info", "任务已继续，请确认配置后开始翻译")
                self._save(job)
                return job

            is_download = bool(job.get("url")) and not _source_input(
                job, "primary_srt", "capcut_en",
            )
            if is_download:
                if str(job.get("options", {}).get("youtube_auth", "none")) == "file":
                    job.update(
                        status="failed",
                        stage="Download failed",
                        error="临时 YouTube Cookie 已在暂停时销毁，请重新提供后重试",
                        error_code="youtube_auth_required",
                    )
                    job["updated_at"] = now()
                    self._append_event(
                        job,
                        "error",
                        "为保护登录凭据，暂停时已销毁临时 Cookie，请重新提供后重试",
                    )
                    self._save(job)
                    return job
                generation = job.get("generation", 0) + 1
                job.update(
                    status="queued", stage="Download retry queued",
                    generation=generation, resume=True,
                    progress={"completed": 0, "total": 0},
                )
                job["updated_at"] = now()
                self._append_event(job, "info", "任务已继续下载")
                self._save(job)
                submit = (self._run_download, job_id, "", generation)
            else:
                options = self._normalize_translation_options(job.get("options", {}))
                api_key = self.secrets.get(job_id, "")
                if not options["dry_run"] and not api_key:
                    job.update(
                        status="ready_for_translation",
                        stage="Paused — API key required to resume",
                    )
                    job["updated_at"] = now()
                    self._append_event(job, "info", "请重新输入 API Key 后继续翻译")
                    self._save(job)
                    return job
                generation = job.get("generation", 0) + 1
                job.update(
                    status="queued", stage="Translation queued",
                    generation=generation, resume=True,
                    progress={"completed": 0, "total": 0},
                    options=options,
                )
                job["updated_at"] = now()
                self._append_event(job, "info", "任务已继续，将从已完成批次恢复")
                self._save(job)
                submit = (self._run, job_id, api_key, generation)

            self._submit(job_id, *submit)
        return self.get(job_id)

    def force_reset(self, job_id: str) -> dict:
        """Force reset a stuck job to failed state so it can be retried or deleted."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] not in ("running", "queued"):
                raise ValueError("只有卡住的任务才需要强制重置")
            job["status"] = "failed"
            job["stage"] = "Force reset (was stuck)"
            job["error"] = "任务卡住，已被强制重置"
            job["error_code"] = "force_reset"
            job["generation"] = job.get("generation", 0) + 1
            job.setdefault("options", {})["api_key_configured"] = False
            job["updated_at"] = now()
            self._append_event(job, "error", "任务卡住，已被强制重置为失败状态")
            self._save(job)
            self.secrets.pop(job_id, None)
            self.private_cookie_store.discard(job_id)
            future = self.futures.get(job_id)
            if future is not None:
                future.cancel()
            return job

    def retry_failed(self, job_id: str, secret: str,
                     restart: bool = False,
                     translation_options: dict[str, Any] | None = None,
                     youtube_auth: str | None = None,
                     youtube_cookie: bytes | bytearray | None = None) -> dict:
        """Mark failed batches as queued again for retry.
        translation_options may include model/base_url to switch API provider."""
        with self.lock:
            self._require_accepting_work()
            job = self._load(job_id)
            completed_dry_run = (
                job["status"] == "completed"
                and bool(job.get("options", {}).get("dry_run"))
                and restart
            )
            if (job["status"] not in ("failed", "ready_for_translation")
                    and not completed_dry_run):
                raise ValueError("只能重试失败或待翻译的任务")

            is_download_retry = (
                job["status"] == "failed"
                and job.get("url")
                and not _source_input(job, "primary_srt", "capcut_en")
            )
            candidate_updates = dict(translation_options or {})

            candidate_options = dict(job.get("options", {}))
            if not is_download_retry:
                candidate_options = self._normalize_translation_options(
                    job.get("options", {}), candidate_updates
                )

            effective_secret = secret or self.secrets.get(job_id, "")
            if (not is_download_retry and not candidate_options.get("dry_run")
                    and not effective_secret):
                raise ValueError("请重新输入 API key 后再重试")

            changed = [
                key for key in (
                    "model", "round2_model", "base_url", "batch_size", "game",
                    "local_whisper", "dry_run", "source_language",
                    "target_language",
                )
                if candidate_options.get(key) != job.get("options", {}).get(key)
            ]

            if is_download_retry:
                if youtube_auth is not None:
                    if youtube_auth not in {"none", "chrome", "file"}:
                        raise ValueError("无效的 YouTube 认证方式")
                    if youtube_auth == "file":
                        if not youtube_cookie:
                            raise ValueError("请粘贴 cookies.txt 内容")
                    else:
                        self.private_cookie_store.discard(job_id)
                    job.setdefault("options", {})["youtube_auth"] = youtube_auth
                generation = job.get("generation", 0) + 1
                effective_auth = str(
                    job.setdefault("options", {}).get("youtube_auth", "none")
                )
                if effective_auth == "file":
                    if not youtube_cookie:
                        raise ValueError("请重新提供 cookies.txt 内容")
                    self.private_cookie_store.queue(
                        job_id, generation, youtube_cookie
                    )
                job.update(
                    status="queued", stage="Download retry queued", error=None,
                    error_code=None, generation=generation,
                    progress={"completed": 0, "total": 0},
                )
                job.setdefault("options", {})["api_key_configured"] = False
                self._append_event(job, "info", "下载任务已重新排队")
                try:
                    self._save(job)
                    self._submit(
                        job_id, self._run_download, job_id, "", generation
                    )
                except BaseException:
                    self.private_cookie_store.discard(job_id, generation)
                    raise
                return self.get(job_id)

            output_dir = self._workspace(job_id)
            manifest_candidates = [
                output_dir / "final.round1.zh.srt.manifest.json",
                output_dir / "final.zh.srt.manifest.json",
            ]
            manifest = next(
                (path for path in manifest_candidates if path.exists()),
                manifest_candidates[0],
            )
            resume_sensitive = {
                "batch_size", "game", "local_whisper", "dry_run",
                "source_language", "target_language",
            }
            incompatible = resume_sensitive.intersection(changed)
            if not restart and manifest.exists() and incompatible:
                labels = ", ".join(sorted(incompatible))
                raise ValueError(f"修改 {labels} 后必须选择从头重跑")
            if (candidate_options.get("local_whisper")
                    and not job.get("inputs", {}).get("video")
                    and not job.get("artifacts", {}).get("video")):
                raise ValueError("局部 Whisper 复核需要已上传或已下载的视频")

            if completed_dry_run:
                dry_run_backups = {
                    "final.round1.zh.srt": "final.round1.dry-run.zh.srt",
                    "final.round2.zh.srt": "final.round2.dry-run.zh.srt",
                    "final.zh.srt": "final.dry-run.zh.srt",
                }
                for source_name, backup_name in dry_run_backups.items():
                    source = output_dir / source_name
                    backup = output_dir / backup_name
                    if source.is_file() and not backup.exists():
                        shutil.copy2(source, backup)
                job["status"] = "ready_for_translation"
                job["stage"] = "Ready for formal translation"
                job["error"] = None
                job["error_code"] = None
                self._append_event(
                    job, "info",
                    "试运行占位产物已保留，任务将使用现有素材开始正式翻译",
                )
                self._save(job)
            elif job["status"] == "failed":
                job["status"] = "ready_for_translation"
                job["stage"] = "Ready for translation (retry)"
                job["error"] = None
                job["error_code"] = None
                if changed:
                    self._append_event(job, "info", f"任务已重置为可重试状态，已更新: {', '.join(changed)}")
                else:
                    self._append_event(job, "info", "任务已重置为可重试状态")
                self._save(job)
            elif changed:
                self._append_event(job, "info", f"已更新: {', '.join(changed)}")
                self._save(job)

            if restart and manifest.exists():
                for stage_manifest in (
                    *manifest_candidates,
                    output_dir / "final.round2.zh.srt.manifest.json",
                ):
                    if stage_manifest.exists():
                        stage_manifest.unlink()
                self._event(job_id, "info", "已清除旧的 manifest，将从头翻译")
            if restart:
                job = self._load(job_id)
                job.pop("config_snapshot", None)
                self._save(job)

            if translation_options is None:
                return self.start_translation(job_id, secret, resume=not restart)
            return self.start_translation(
                job_id, secret, resume=not restart,
                translation_options=candidate_options,
            )

    def delete(self, job_id: str) -> dict:
        """Permanently remove a job and all its files.
        Only allowed for non-running jobs."""
        with self.lock:
            job = self._load(job_id)
            if job["status"] in ("running", "queued"):
                raise ValueError("任务正在运行，请先取消")
            if self._burn_export_active(job):
                raise ValueError("带字幕视频正在生成，请先取消")
            future = self.futures.get(job_id)
            if future is not None and not future.done():
                raise ValueError("后台任务仍在停止，请稍后再删除")
            workspace = self._workspace(job_id)
            import gc
            import time
            last_error = None
            for attempt in range(6):
                try:
                    shutil.rmtree(workspace, ignore_errors=False)
                    last_error = None
                    break
                except PermissionError as error:
                    last_error = error
                    gc.collect()
                    time.sleep(0.1 * (attempt + 1))
            if last_error is not None:
                raise ValueError("任务文件仍被后台进程占用，请稍后再删除") from last_error
            self.secrets.pop(job_id, None)
            self.private_cookie_store.discard(job_id)
            self.futures.pop(job_id, None)
            self.burn_futures.pop(job_id, None)
            self.burn_cancel_events.pop(job_id, None)
            self._job_change_cache.invalidate(job_id)
            self._burn_fingerprint_cache.pop(job_id, None)
            return {"deleted": job_id}


manager = JobManager()
