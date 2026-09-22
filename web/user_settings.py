"""Persistent non-secret preferences and OS-backed API credentials."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol


SETTINGS_VERSION = 1
DEFAULT_PROXY = "http://127.0.0.1:7890"
CREDENTIAL_TARGET = "PhoebeSubtitleLab/APIKey"
NON_SECRET_FIELDS = {
    "proxy",
    "base_url",
    "model",
    "round2_model",
    "batch_size",
    "local_whisper",
}
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    "proxy": DEFAULT_PROXY,
    "base_url": "",
    "model": "",
    "round2_model": "",
    "batch_size": 28,
    "local_whisper": False,
}
SESSION_ONLY_WARNING = (
    "系统安全存储不可用，API Key 仅本次会话有效；关闭程序后需要重新输入"
)
SECURE_READ_WARNING = "系统安全存储不可用，暂时无法读取已保存的 API Key"


class CredentialStore(Protocol):
    def read(self) -> str: ...

    def write(self, secret: str) -> None: ...

    def delete(self) -> None: ...


class CredentialStoreUnavailable(RuntimeError):
    """A safe public error for an unavailable OS credential backend."""

    def __init__(self) -> None:
        super().__init__("系统安全存储不可用，无法完成凭据操作")


class WindowsCredentialStore:
    """Store one UTF-16 API key as a Windows Generic Credential."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self, target: str = CREDENTIAL_TARGET):
        self.target = target

    @staticmethod
    def _advapi32():
        if os.name != "nt":
            raise OSError("Windows Credential Manager is unavailable")
        return ctypes.WinDLL("Advapi32.dll", use_last_error=True)

    def read(self) -> str:
        api = self._advapi32()
        pointer = ctypes.POINTER(self._CREDENTIALW)()
        api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(self._CREDENTIALW)),
        ]
        api.CredReadW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        if not api.CredReadW(
            self.target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return ""
            raise OSError(error, "Credential Manager read failed")
        try:
            credential = pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return blob.decode("utf-16-le")
        finally:
            api.CredFree(pointer)

    def write(self, secret: str) -> None:
        api = self._advapi32()
        blob = secret.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self._CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_ubyte)
        )
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "PhoebeSubtitleLab"
        api.CredWriteW.argtypes = [ctypes.POINTER(self._CREDENTIALW), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise OSError(ctypes.get_last_error(), "Credential Manager write failed")

    def delete(self) -> None:
        api = self._advapi32()
        api.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        api.CredDeleteW.restype = wintypes.BOOL
        if not api.CredDeleteW(self.target, self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self.ERROR_NOT_FOUND:
                raise OSError(error, "Credential Manager delete failed")


def default_settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "PhoebeSubtitleLab" / "settings.json"
    return Path.home() / ".local" / "share" / "PhoebeSubtitleLab" / "settings.json"


class UserSettingsStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        credential_store: CredentialStore | None = None,
    ):
        self.path = Path(path) if path is not None else default_settings_path()
        self.credential_store = credential_store or WindowsCredentialStore()
        self._lock = threading.RLock()
        self._session_api_key = ""
        self._credential_warning = ""

    @staticmethod
    def _normalize(raw: Any) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        if not isinstance(raw, dict) or raw.get("version") != SETTINGS_VERSION:
            return result
        for field in ("proxy", "base_url", "model", "round2_model"):
            value = raw.get(field)
            if isinstance(value, str):
                result[field] = value
        batch_size = raw.get("batch_size")
        if isinstance(batch_size, int) and not isinstance(batch_size, bool):
            if 1 <= batch_size <= 100:
                result["batch_size"] = batch_size
        local_whisper = raw.get("local_whisper")
        if isinstance(local_whisper, bool):
            result["local_whisper"] = local_whisper
        return result

    def _read_non_secret(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return dict(DEFAULT_SETTINGS)
        return self._normalize(raw)

    def _write_non_secret(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(
            self._normalize(settings), ensure_ascii=False, indent=2
        ) + "\n"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_secure_key(self) -> str:
        try:
            secret = self.credential_store.read().strip()
        except OSError:
            self._credential_warning = SECURE_READ_WARNING
            return ""
        if self._credential_warning == SECURE_READ_WARNING:
            self._credential_warning = ""
        return secret

    def resolve_api_key(self, transient: str = "") -> str:
        with self._lock:
            candidate = str(transient or "").strip()
            if candidate:
                return candidate
            if self._session_api_key:
                return self._session_api_key
            return self._read_secure_key()

    def get_public_settings(self) -> dict[str, Any]:
        with self._lock:
            settings = self._read_non_secret()
            secure_key = "" if self._session_api_key else self._read_secure_key()
            configured = bool(self._session_api_key or secure_key)
            persistence = (
                "session"
                if self._session_api_key
                else "secure"
                if secure_key
                else "none"
            )
            return {
                **settings,
                "api_key_configured": configured,
                "api_key_persistence": persistence,
                "credential_warning": self._credential_warning,
            }

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("设置必须是对象")
        with self._lock:
            settings = self._read_non_secret()
            for field in NON_SECRET_FIELDS:
                if field in updates:
                    settings[field] = updates[field]
            settings = self._normalize(settings)
            self._write_non_secret(settings)

            secret = str(updates.get("api_key") or "").strip()
            if secret:
                try:
                    self.credential_store.write(secret)
                    self._session_api_key = ""
                    self._credential_warning = ""
                except OSError:
                    self._session_api_key = secret
                    self._credential_warning = SESSION_ONLY_WARNING
            return self.get_public_settings()

    def delete_api_key(self) -> dict[str, Any]:
        with self._lock:
            session_only = bool(self._session_api_key) and (
                self._credential_warning == SESSION_ONLY_WARNING
            )
            self._session_api_key = ""
            try:
                self.credential_store.delete()
            except OSError as error:
                if session_only:
                    self._credential_warning = SECURE_READ_WARNING
                    return self.get_public_settings()
                self._credential_warning = "系统安全存储不可用，无法删除已保存的 API Key"
                raise CredentialStoreUnavailable() from error
            self._credential_warning = ""
            return self.get_public_settings()
