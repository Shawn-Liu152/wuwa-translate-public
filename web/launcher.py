"""Visible Web launcher that opens the browser only after Uvicorn is ready."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import tempfile
from pathlib import Path

import uvicorn


HOST = "127.0.0.1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = (PROJECT_ROOT / "VERSION").read_text(
    encoding="utf-8"
).strip()
INSTANCE_STATE_PATH = PROJECT_ROOT / ".runtime" / "workbench-instance.json"


def _instance_url(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != 1:
        return None
    if payload.get("project_version") != PROJECT_VERSION:
        return None
    value = payload.get("url")
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != HOST
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path != "/app"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"http://{HOST}:{port}/app"


def reopen_existing_workbench(state_path: Path = INSTANCE_STATE_PATH) -> bool:
    """Reopen a healthy instance without persisting any authentication token."""
    try:
        if state_path.is_symlink() or state_path.stat().st_size > 4096:
            return False
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        url = _instance_url(payload)
        if url is None:
            return False
        parsed = urllib.parse.urlparse(url)
        request = urllib.request.Request(
            f"http://{HOST}:{parsed.port}/api/health",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=0.5) as response:
            if response.status != 200:
                return False
            raw = response.read(4097)
        if len(raw) > 4096:
            return False
        health = json.loads(raw.decode("utf-8"))
        if not isinstance(health, dict):
            return False
        if health.get("ok") is not True:
            return False
        if health.get("version") != PROJECT_VERSION:
            return False
        webbrowser.open(url)
        return True
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
            urllib.error.URLError):
        return False


def _write_instance_state(state_path: Path, port: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "pid": os.getpid(),
        "url": f"http://{HOST}:{port}/app",
        "project_version": PROJECT_VERSION,
    }
    temporary = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True),
        encoding="utf-8",
    )
    os.replace(temporary, state_path)


def _remove_instance_state(state_path: Path, port: int) -> None:
    try:
        if state_path.is_symlink():
            return
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if payload.get("pid") != os.getpid():
            return
        if _instance_url(payload) != f"http://{HOST}:{port}/app":
            return
        state_path.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return


def prepare_portable_runtime() -> None:
    """Expose bundled command-line helpers and reject read-only locations.

    The portable ZIP deliberately stores jobs and user-edited data beside the
    application.  Failing here gives a useful message instead of a later
    SQLite or download-directory traceback.
    """
    if not os.getenv("SUBTITLE_PORTABLE"):
        return

    bundled_bin = PROJECT_ROOT / "runtime" / "bin"
    if bundled_bin.is_dir():
        os.environ["PATH"] = str(bundled_bin) + os.pathsep + os.environ.get(
            "PATH", ""
        )

    try:
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".subtitle-portable-write-", dir=PROJECT_ROOT
        )
        os.close(descriptor)
        Path(probe_name).unlink()
    except OSError as error:
        raise SystemExit(
            "便携版所在目录不可写。请先完整解压到桌面或文档目录，"
            "不要直接在 ZIP 或 Program Files 中运行。"
        ) from error


def _bind_loopback_socket() -> socket.socket:
    """Reserve a random loopback port before any browser can be opened."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        listener.bind((HOST, 0))
        listener.listen(2048)
        listener.set_inheritable(False)
        return listener
    except BaseException:
        listener.close()
        raise


def open_browser_when_ready(
    server: uvicorn.Server,
    port: int,
    bootstrap_token: str,
    launch_challenge: str,
    *,
    instance_path: Path | None = None,
    open_browser: bool = True,
) -> None:
    """Open only after this exact Uvicorn instance proves it is ready."""
    ready_url = f"http://{HOST}:{port}/api/launcher-ready"
    for _ in range(100):
        if server.should_exit:
            return
        if not server.started:
            time.sleep(0.1)
            continue
        try:
            request = urllib.request.Request(
                ready_url,
                headers={"X-Subtitle-Launch-Challenge": launch_challenge},
            )
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if response.status != 204:
                    time.sleep(0.1)
                    continue
                if instance_path is not None:
                    _write_instance_state(instance_path, port)
                if not open_browser:
                    return
                fragment = urllib.parse.quote(bootstrap_token, safe="")
                webbrowser.open(
                    f"http://{HOST}:{port}/app#bootstrap={fragment}"
                )
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", default="")
    return parser.parse_args()


def _validate_project_root(value: str) -> None:
    if not value:
        return
    try:
        supplied = Path(value).resolve()
    except OSError as error:
        raise SystemExit(f"Invalid project root: {error}") from error
    if supplied != PROJECT_ROOT:
        raise SystemExit("Launcher project root does not match this installation")


def main() -> None:
    args = _parse_args()
    _validate_project_root(args.project_root)
    prepare_portable_runtime()
    if (
        not os.getenv("SUBTITLE_NO_BROWSER")
        and reopen_existing_workbench(INSTANCE_STATE_PATH)
    ):
        return
    listener = _bind_loopback_socket()
    port = int(listener.getsockname()[1])
    session_token = secrets.token_urlsafe(32)
    bootstrap_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    launch_challenge = secrets.token_urlsafe(32)
    from web import app as app_module
    app_module.configure_local_access(
        session_token=session_token,
        bootstrap_token=bootstrap_token,
        csrf_token=csrf_token,
        launch_challenge=launch_challenge,
    )
    config = uvicorn.Config(
        app_module.app,
        host=HOST,
        port=port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )
    server = uvicorn.Server(config)
    threading.Thread(
        target=open_browser_when_ready,
        args=(server, port, bootstrap_token, launch_challenge),
        kwargs={
            "instance_path": INSTANCE_STATE_PATH,
            "open_browser": not bool(os.getenv("SUBTITLE_NO_BROWSER")),
        },
        name="subtitle-browser-launcher",
        daemon=True,
    ).start()
    try:
        server.run(sockets=[listener])
    finally:
        _remove_instance_state(INSTANCE_STATE_PATH, port)
        listener.close()


if __name__ == "__main__":
    main()
