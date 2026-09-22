import json
import socket

import pytest

from web import launcher


def test_repeated_launch_reopens_healthy_existing_workbench(
        tmp_path, monkeypatch):
    project_version = (launcher.PROJECT_ROOT / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    state_path = tmp_path / "workbench-instance.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "pid": 1234,
        "url": "http://127.0.0.1:54321/app",
        "project_version": project_version,
    }), encoding="utf-8")

    class HealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "ok": True,
                "version": project_version,
            }).encode("utf-8")

    requests = []
    opened = []
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda request, timeout: requests.append((request, timeout))
        or HealthResponse(),
    )
    monkeypatch.setattr(
        launcher.webbrowser, "open", lambda url: opened.append(url)
    )

    assert launcher.reopen_existing_workbench(state_path) is True
    assert requests[0][0].full_url == (
        "http://127.0.0.1:54321/api/health"
    )
    assert opened == ["http://127.0.0.1:54321/app"]
    assert "bootstrap" not in state_path.read_text(encoding="utf-8")


def test_repeated_launch_ignores_malformed_health_response(
        tmp_path, monkeypatch):
    project_version = (launcher.PROJECT_ROOT / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    state_path = tmp_path / "workbench-instance.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "pid": 1234,
        "url": "http://127.0.0.1:54321/app",
        "project_version": project_version,
    }), encoding="utf-8")

    class InvalidHealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"[]"

    opened = []
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda _request, timeout: InvalidHealthResponse(),
    )
    monkeypatch.setattr(
        launcher.webbrowser, "open", lambda url: opened.append(url)
    )

    assert launcher.reopen_existing_workbench(state_path) is False
    assert opened == []


def test_launcher_faq_explains_repeated_launch_behavior():
    page = (launcher.PROJECT_ROOT / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "重复双击启动器会重新打开现有页面" in page


def test_launcher_reserves_an_exclusive_random_loopback_socket():
    listener = launcher._bind_loopback_socket()
    try:
        host, port = listener.getsockname()
        assert host == "127.0.0.1"
        assert 0 < port < 65536
        contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                contender.bind((host, port))
        finally:
            contender.close()
    finally:
        listener.close()


def test_launcher_opens_only_the_authenticated_workbench(monkeypatch):
    class Server:
        started = True
        should_exit = False

    class ReadyResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requests = []
    opened = []
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda request, timeout: requests.append((request, timeout))
        or ReadyResponse(),
    )
    monkeypatch.setattr(
        launcher.webbrowser, "open", lambda url: opened.append(url)
    )

    launcher.open_browser_when_ready(
        Server(), 54321, "bootstrap-value", "launch-challenge-value"
    )

    assert len(requests) == 1
    assert requests[0][0].full_url == (
        "http://127.0.0.1:54321/api/launcher-ready"
    )
    assert requests[0][0].get_header("X-subtitle-launch-challenge") == (
        "launch-challenge-value"
    )
    assert opened == [
        "http://127.0.0.1:54321/app#bootstrap=bootstrap-value"
    ]
