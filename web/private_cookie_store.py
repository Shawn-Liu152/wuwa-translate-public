"""Crash-resilient temporary storage for user-supplied YouTube cookies."""
from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class CookieCredentialUnavailable(RuntimeError):
    """Raised when a queued file-auth download no longer has its credential."""


def _zero(payload: bytearray | None) -> None:
    if payload is not None:
        payload[:] = b"\0" * len(payload)
        payload.clear()


def _unlink_best_effort(path: Path, *, attempts: int = 4) -> bool:
    """Retry transient Windows handle contention without masking shutdown."""
    for attempt in range(max(1, attempts)):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    try:
        return not path.exists()
    except OSError:
        return False


def default_private_cookie_root() -> Path:
    """Use the OS per-user local-data directory, not the portable/job tree."""
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, 0x001C, None, 0, buffer  # CSIDL_LOCAL_APPDATA
        )
        if result != 0 or not buffer.value:
            raise RuntimeError("无法定位 Windows 当前用户的本地应用数据目录")
        base = Path(buffer.value)
    else:
        uid = getattr(os, "getuid", lambda: 0)()
        base = Path(tempfile.gettempdir()) / f"phoebe-subtitle-{uid}"
    return base / "PhoebeSubtitleLab" / "PrivateTemp" / "cookies"


def _has_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _windows_current_sid() -> str:
    """Return the current process TokenUser SID using trusted Win32 APIs."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise OSError("OpenProcessToken failed")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, 1, None, 0, ctypes.byref(size)  # TokenUser
        )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, size, ctypes.byref(size)
        ):
            raise OSError("GetTokenInformation failed")

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("sid", ctypes.c_void_p),
                ("attributes", wintypes.DWORD),
            ]

        class TokenUser(ctypes.Structure):
            _fields_ = [("user", SidAndAttributes)]

        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            user.user.sid, ctypes.byref(sid_text)
        ):
            raise OSError("ConvertSidToStringSid failed")
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _windows_system_tool(name: str) -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("GetSystemDirectory failed")
    tool = Path(buffer.value) / name
    if not tool.is_file():
        raise OSError(f"Windows system tool is unavailable: {name}")
    return tool


def _restrict_windows_path(path: Path, *, directory: bool) -> None:
    """Apply a protected DACL before any credential bytes are written."""
    sid = _windows_current_sid()
    grant = f"*{sid}:(OI)(CI)(F)" if directory else f"*{sid}:(F)"
    result = subprocess.run(
        [
            str(_windows_system_tool("icacls.exe")),
            str(path),
            "/inheritance:r",
            "/grant:r",
            grant,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise OSError("无法将临时凭据权限限制为当前 Windows 用户")


class PrivateCookieStore:
    """Keep queued cookies in memory and materialize only for yt-dlp."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        legacy_jobs_root: Path | None = None,
        enforce_os_acl: bool = True,
    ) -> None:
        self.root = Path(root) if root is not None else default_private_cookie_root()
        self.legacy_jobs_root = legacy_jobs_root
        self.enforce_os_acl = enforce_os_acl
        self._pending: dict[tuple[str, int], bytearray] = {}
        self._materialized: set[Path] = set()
        self._state_lock = threading.RLock()
        self._lock_handle = None
        self._initialized = False

    def initialize(self) -> None:
        with self._state_lock:
            if self._initialized:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            if _has_reparse_point(self.root):
                raise RuntimeError("临时凭据目录不能是符号链接或目录联接")
            if os.name == "nt" and self.enforce_os_acl:
                _restrict_windows_path(self.root, directory=True)
            else:
                self.root.chmod(0o700)
            self._acquire_process_lock()
            try:
                self._cleanup_stale_files()
                self._cleanup_legacy_job_files()
                if self.enforce_os_acl:
                    self._cleanup_legacy_system_temp()
            except BaseException:
                self._release_process_lock()
                raise
            self._initialized = True

    def _acquire_process_lock(self) -> None:
        lock_path = self.root / ".instance.lock"
        handle = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            handle.close()
            raise RuntimeError("同一安装目录的另一个工作台实例正在运行") from error
        self._lock_handle = handle

    def _release_process_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _cleanup_stale_files(self) -> None:
        for path in self.root.glob("subtitle-cookie-*.cookie"):
            if path.is_file() and not _has_reparse_point(path):
                path.unlink(missing_ok=True)

    def _cleanup_legacy_job_files(self) -> None:
        if self.legacy_jobs_root is None:
            return
        for path in self.legacy_jobs_root.glob("*/youtube.cookies.txt"):
            if path.is_file() and not _has_reparse_point(path):
                path.unlink(missing_ok=True)

    @staticmethod
    def _looks_like_netscape_cookie(path: Path) -> bool:
        try:
            first = path.open("rb").read(64).decode(
                "utf-8-sig", errors="replace"
            ).splitlines()[0].strip()
        except (OSError, IndexError):
            return False
        return first in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}

    def _cleanup_legacy_system_temp(self) -> None:
        for path in Path(tempfile.gettempdir()).glob("subtitle-cookie-*.txt"):
            if (
                path.is_file()
                and not _has_reparse_point(path)
                and self._looks_like_netscape_cookie(path)
            ):
                path.unlink(missing_ok=True)

    def queue(self, job_id: str, generation: int, payload: bytes | bytearray) -> None:
        self.initialize()
        key = (job_id, int(generation))
        value = bytearray(payload)
        if not value:
            raise ValueError("Cookie 内容不能为空")
        with self._state_lock:
            _zero(self._pending.pop(key, None))
            self._pending[key] = value

    def has_pending(self, job_id: str, generation: int) -> bool:
        with self._state_lock:
            return (job_id, int(generation)) in self._pending

    def materialize(self, job_id: str, generation: int) -> Path:
        """Create a current-user-only file immediately before yt-dlp starts."""
        self.initialize()
        key = (job_id, int(generation))
        with self._state_lock:
            payload = self._pending.pop(key, None)
        if payload is None:
            raise CookieCredentialUnavailable(
                "临时 YouTube Cookie 已在暂停或重启时销毁，请重新提供后重试"
            )
        descriptor, raw_path = tempfile.mkstemp(
            prefix="subtitle-cookie-", suffix=".cookie", dir=self.root
        )
        path = Path(raw_path)
        try:
            if _has_reparse_point(path):
                raise RuntimeError("临时凭据文件不能是重解析点")
            if os.name == "nt" and self.enforce_os_acl:
                _restrict_windows_path(path, directory=False)
            else:
                os.chmod(path, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            with self._state_lock:
                self._materialized.add(path)
            return path
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        finally:
            _zero(payload)

    def cleanup_materialized(self, path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        removed = _unlink_best_effort(path)
        with self._state_lock:
            if removed:
                self._materialized.discard(path)
            else:
                self._materialized.add(path)

    def discard(self, job_id: str, generation: int | None = None) -> None:
        with self._state_lock:
            keys = [
                key for key in self._pending
                if key[0] == job_id and (
                    generation is None or key[1] == int(generation)
                )
            ]
            for key in keys:
                _zero(self._pending.pop(key, None))

    def close(self) -> None:
        with self._state_lock:
            for payload in self._pending.values():
                _zero(payload)
            self._pending.clear()
            paths = list(self._materialized)
            self._materialized.clear()
            for path in paths:
                if not _unlink_best_effort(path):
                    self._materialized.add(path)
            self._initialized = False
            try:
                self._release_process_lock()
            except OSError:
                # Closing the handle in _release_process_lock still releases the
                # OS lock; stale files are retried on the next initialize().
                pass
