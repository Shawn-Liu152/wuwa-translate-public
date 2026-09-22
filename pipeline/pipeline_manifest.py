"""Persistent, atomic state for resumable long-video subtitle workflows."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, Dict, Iterable

from pipeline.languages import normalize_language_pair
from pipeline.safe_errors import public_error_fields

SCHEMA_VERSION = 1
BATCH_STATES = {"pending", "running", "success", "failed"}


class PipelineCancelled(RuntimeError):
    """Stop pipeline work without treating cancellation as a batch failure."""


def raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise PipelineCancelled("Task cancelled")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_manifest(input_path: str, options: Dict[str, Any], entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    options = {**options, **normalize_language_pair(options)}
    return {
        "schema_version": SCHEMA_VERSION,
        "input": {"path": Path(input_path).name, "sha256": file_sha256(input_path)},
        "options": options,
        "entries": list(entries),
        "batches": {},
        "artifacts": {},
        "status": "running",
        "updated_at": utc_now(),
    }


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as source:
        manifest = json.load(source)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("manifest schema version 不兼容")
    manifest["options"] = {
        **(manifest.get("options") or {}),
        **normalize_language_pair(manifest.get("options") or {}),
    }
    _sanitize_manifest_public_fields(manifest)
    return manifest


def validate_resume(manifest: Dict[str, Any], input_path: str, options: Dict[str, Any]) -> None:
    if manifest["input"]["sha256"] != file_sha256(input_path):
        raise ValueError("输入文件已变化，不能 resume")
    options = {**options, **normalize_language_pair(options)}
    prior = {
        **(manifest.get("options") or {}),
        **normalize_language_pair(manifest.get("options") or {}),
    }
    if prior != options:
        mismatched = [key for key, value in options.items() if prior.get(key) != value]
        # Resume is intentionally allowed after a prior failed batch: the operator may
        # change backend/model while preserving source text and completed results.
        # llm_disable_thinking 是 O-2 的上游 thinking 关闭生效值（探测结果随站点
        # 变化），属后端能力标志而非翻译语义参数，差异不阻断 resume。
        allowed = {"model", "llm_disable_thinking"}
        if any(key not in allowed for key in mismatched):
            raise ValueError("翻译参数已变化，不能 resume")


def save_manifest(path: str, manifest: Dict[str, Any]) -> None:
    _sanitize_manifest_public_fields(manifest)
    manifest["updated_at"] = utc_now()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, target)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def set_batch(manifest: Dict[str, Any], batch_id: str, state: str, **data: Any) -> None:
    if state not in BATCH_STATES:
        raise ValueError(f"未知批次状态: {state}")
    previous = dict(manifest["batches"].get(batch_id, {}))
    if state in {"running", "success"} and "error" not in data:
        for key in ("error", "error_code", "upstream_status"):
            previous.pop(key, None)
    if "error" in data:
        fields = public_error_fields(
            data.get("error_code"),
            data.get("upstream_status"),
            fallback_code="batch_processing_error",
        )
        data = {
            **data,
            "error": fields["message"],
            "error_code": fields["error_code"],
            "upstream_status": fields["upstream_status"],
        }
    attempts = previous.get("attempts", 0) + (
        1
        if state == "running" and previous.get("state") != "running"
        else 0
    )
    manifest["batches"][batch_id] = {
        **previous, **data, "state": state, "attempts": attempts, "updated_at": utc_now()
    }


def _sanitize_manifest_public_fields(manifest: Dict[str, Any]) -> None:
    """Remove private paths and normalize every persisted batch error in place."""
    input_info = manifest.get("input")
    if isinstance(input_info, dict) and input_info.get("path"):
        input_info["path"] = Path(str(input_info["path"])).name

    artifacts = manifest.get("artifacts") or {}
    if isinstance(artifacts, dict):
        for artifact in artifacts.values():
            if isinstance(artifact, dict) and artifact.get("path"):
                artifact["path"] = Path(str(artifact["path"])).name

    batches = manifest.get("batches") or {}
    if not isinstance(batches, dict):
        return
    for batch in batches.values():
        if not isinstance(batch, dict) or "error" not in batch:
            continue
        fields = public_error_fields(
            batch.get("error_code"),
            batch.get("upstream_status"),
            fallback_code="batch_processing_error",
        )
        batch["error"] = fields["message"]
        batch["error_code"] = fields["error_code"]
        batch["upstream_status"] = fields["upstream_status"]


class ThrottledManifestWriter:
    """Coalesce full-manifest rewrites for long jobs (fixes O(n^2) write cost).

    每个成功批次都整份 JSON 重写 manifest 会随批次数线性变慢，并且
    全程在调用方锁内执行、串行化并发批次（2026-09-03 实测：2000 条
    63 批合计 1229ms，单次写从 2.5ms 涨到 19.2ms）。

    策略：
    - failed / cancelled（pending）状态立即落盘——失败原因必须持久化；
    - running / success 状态累计 ``flush_every`` 批或距上次写超过
      ``flush_interval`` 秒才落盘；
    - 调用方在终态（completed/failed/validation 失败）和批处理循环
      结束后必须调用 :meth:`flush` 强制落盘。

    崩溃窗口：进程被硬杀时，最近未落盘的批次在 resume 时会重译
    （最多 ``flush_every - 1`` 批），语义与逐批落盘兼容。
    """

    def __init__(
        self,
        path: str,
        manifest: Dict[str, Any],
        *,
        lock: threading.RLock | None = None,
        flush_every: int = 8,
        flush_interval: float = 30.0,
    ) -> None:
        self._path = path
        self._manifest = manifest
        self._lock = lock or threading.RLock()
        self._flush_every = max(1, int(flush_every))
        self._flush_interval = max(0.0, float(flush_interval))
        self._dirty = 0
        self._last_flush = time.monotonic()

    @property
    def pending_batches(self) -> int:
        """尚未落盘的 set_batch 次数（供测试与诊断）。"""
        with self._lock:
            return self._dirty

    def set_batch(self, batch_id: str, state: str, **data: Any) -> None:
        with self._lock:
            set_batch(self._manifest, batch_id, state, **data)
            self._dirty += 1
            if (
                state in {"failed", "pending"}
                or self._dirty >= self._flush_every
                or time.monotonic() - self._last_flush >= self._flush_interval
            ):
                self.flush()

    def flush(self, *, force: bool = False) -> None:
        """把累积的批次状态写入磁盘。

        无脏数据且 force=False 时不产生写放大；force=True 用于
        调用方修改了 manifest 顶层字段（status/metrics）后的强制落盘。
        """
        with self._lock:
            try:
                if self._dirty or force:
                    save_manifest(self._path, self._manifest)
                    self._dirty = 0
            finally:
                self._last_flush = time.monotonic()


def successful_batch_results(manifest: Dict[str, Any], batch_id: str) -> list[Dict[str, Any]] | None:
    batch = manifest.get("batches", {}).get(batch_id)
    if not batch or batch.get("state") != "success":
        return None
    results = batch.get("results") or []
    expected_ids = {int(item) for item in batch.get("ids", [])}
    result_ids = {
        int(item.get("id")) for item in results
        if item.get("id") is not None
        and str(item.get("translated", "")).strip()
    }
    if results and (not expected_ids or result_ids == expected_ids):
        return results
    return None


def partial_batch_results(manifest: Dict[str, Any], batch_id: str) -> list[Dict[str, Any]]:
    """Return non-empty reusable results even when a batch is incomplete."""
    batch = manifest.get("batches", {}).get(batch_id) or {}
    expected_ids = {int(item) for item in batch.get("ids", [])}
    reusable: dict[int, Dict[str, Any]] = {}
    for item in batch.get("results") or []:
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if ((not expected_ids or item_id in expected_ids)
                and str(item.get("translated", "")).strip()):
            reusable[item_id] = item
    return list(reusable.values())


def summarize_manifest_metrics(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate stable batch metrics for the Web dashboard."""
    batches = list((manifest.get("batches") or {}).values())
    successful = [batch for batch in batches if batch.get("state") == "success"]
    metrics = [batch.get("metrics") or {} for batch in batches]
    glossary_counts = [
        int(item.get("glossary_terms", 0))
        for item in metrics
    ]
    summary = {
        "batch_count": len(batches),
        "successful_batches": len(successful),
        "attempts": sum(int(batch.get("attempts", 0)) for batch in batches),
        "response_attempts": sum(
            int(item.get("response_attempts", 0)) for item in metrics
        ),
        "tokens_in": sum(int(item.get("tokens_in", 0)) for item in metrics),
        "tokens_out": sum(int(item.get("tokens_out", 0)) for item in metrics),
        "cost": round(
            sum(float(item.get("cost", 0.0)) for item in metrics),
            8,
        ),
        "elapsed_seconds": round(
            sum(float(item.get("elapsed_seconds", 0.0)) for item in metrics),
            3,
        ),
        "average_glossary_terms": round(
            sum(glossary_counts) / max(len(glossary_counts), 1),
            2,
        ),
        "max_glossary_terms": max(glossary_counts, default=0),
    }
    if any("translatable_units" in item for item in metrics):
        summary.update({
            "translatable_units": sum(
                int(item.get("translatable_units", 0)) for item in metrics
            ),
            "glossary_hit_units": sum(
                int(item.get("glossary_hit_units", 0)) for item in metrics
            ),
            "glossary_match_occurrences": sum(
                int(item.get("glossary_match_occurrences", 0)) for item in metrics
            ),
        })
    return summary
