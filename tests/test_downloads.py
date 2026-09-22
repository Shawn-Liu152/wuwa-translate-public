import pytest
import io
import sys
from pathlib import Path

from pipeline.config import Config
from pipeline.parser.srt_parser import Subtitle, parse_srt, save_srt
from web.downloads import (
    _pick_source_subtitle,
    _subtitle_language_selector,
    _display_download_line,
    _build_quality_strategies,
    _build_resilience_args,
    _classify_download_failure,
    _summarize_yt_dlp_error,
    prepare_youtube_subtitles,
    run_download,
    video_format_selector,
)


def test_video_format_selector_supports_quality_caps():
    assert video_format_selector("1080") == (
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    )
    assert video_format_selector("480").endswith("best[height<=480]")
    assert video_format_selector("best") == "bestvideo+bestaudio/best"


def test_video_format_selector_rejects_unknown_quality():
    with pytest.raises(ValueError, match="清晰度"):
        video_format_selector("8k-ish")


def test_download_progress_marker_is_localised_after_process_decoding():
    line = "SUBTITLE_PROGRESS 53.7% | 592.98KiB/s | ETA 01:41"

    displayed = _display_download_line(line)

    assert displayed == "下载进度 53.7% | 592.98KiB/s | ETA 01:41"
    assert "\ufffd" not in displayed


def test_youtube_subtitles_are_cleaned_without_mutating_raw(tmp_path):
    raw = tmp_path / "video.en.srt"
    cleaned = tmp_path / "video.en.cleaned.srt"
    save_srt(str(raw), [
        Subtitle(
            1, "00:00:00,000", "00:00:02,000",
            "Holy [\\h__\\h]\nDude, they are",
        ),
        Subtitle(
            2, "00:00:01,500", "00:00:03,000",
            "Holy [ __ ]\nDude, they are cooking",
        ),
        Subtitle(
            3, "00:00:03,000", "00:00:03,010",
            "Dude, they are cooking",
        ),
        Subtitle(4, "00:00:10,000", "00:00:11,000", r"[\h__\h]"),
    ])

    prepare_youtube_subtitles(raw, cleaned)

    assert r"[\h__\h]" in raw.read_text(encoding="utf-8-sig")
    assert [item.text for item in parse_srt(str(cleaned))] == [
        "Holy\nDude, they are cooking",
    ]


def test_download_requests_and_normalizes_japanese_subtitles(tmp_path, monkeypatch):
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.ja-JP.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
                encoding="utf-8",
            )

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_thumbnail=False,
        source_language="ja",
    )

    command = commands[0]
    assert command[command.index("--sub-lang") + 1] == "ja,ja-JP,ja.*"
    assert Path(artifacts["youtube_source"]).name == "abcdefghijk.ja.cleaned.srt"
    assert artifacts["youtube_source"] == artifacts["youtube_ja"]


def test_download_converts_native_webvtt_without_ffmpeg(tmp_path, monkeypatch):
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.en.vtt").write_text(
                "WEBVTT\n\n"
                "00:00:01.000 --> 00:00:02.500 align:start position:0%\n"
                "<v Speaker>Hello <00:00:01.500><c>world &amp; friends</c>\n\n"
                "cue-2\n"
                "01:02.250 --> 01:04.000\n"
                "Second line\n"
                "Second line\n",
                encoding="utf-8",
            )

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_thumbnail=False,
    )

    command = commands[0]
    assert "--convert-subs" not in command
    assert command[command.index("--sub-format") + 1] == "vtt"
    raw_srt = Path(artifacts["youtube_en_raw"])
    assert raw_srt.suffix == ".srt"
    assert [
        (item.start, item.end, item.text) for item in parse_srt(str(raw_srt))
    ] == [
        ("00:00:01,000", "00:00:02,500", "Hello world & friends"),
        ("00:01:02,250", "00:01:04,000", "Second line"),
    ]
    assert not list(output.glob("*.vtt"))


def test_japanese_subtitle_track_selection_prefers_exact_manual_track(tmp_path):
    manual = tmp_path / "abcdefghijk.ja.srt"
    regional = tmp_path / "abcdefghijk.ja-JP.srt"
    automatic = tmp_path / "abcdefghijk.ja-orig.srt"
    for path in (manual, regional, automatic):
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")

    selected = _pick_source_subtitle(tmp_path, "abcdefghijk", "ja")

    assert selected == manual


def test_korean_selector_accepts_regional_tracks_and_prefers_exact(tmp_path):
    exact = tmp_path / "abcdefghijk.ko.srt"
    regional = tmp_path / "abcdefghijk.ko-KR.srt"
    for path in (exact, regional):
        path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n한국어\n",
            encoding="utf-8",
        )

    assert _subtitle_language_selector("ko") == "ko,ko-KR,ko.*"
    assert _pick_source_subtitle(tmp_path, "abcdefghijk", "ko") == exact


def test_japanese_rolling_caption_cleanup_does_not_require_spaces(tmp_path):
    raw = tmp_path / "video.ja.srt"
    cleaned = tmp_path / "video.ja.cleaned.srt"
    save_srt(str(raw), [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "これは本当に"),
        Subtitle(2, "00:00:01,500", "00:00:03,000", "これは本当にすごい"),
    ])

    prepare_youtube_subtitles(raw, cleaned, source_language="ja")

    assert [item.text for item in parse_srt(str(cleaned))] == ["これは本当にすごい"]


def test_download_uses_uploaded_cookie_file(tmp_path, monkeypatch):
    cookie_file = tmp_path / "youtube.cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\ttest\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.jpg").write_bytes(b"jpg")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_subtitles=False,
        download_thumbnail=True,
        cookies_file=str(cookie_file),
    )

    assert artifacts["thumbnail"].endswith("abcdefghijk.jpg")
    assert "--cookies" in commands[0]
    assert commands[0][commands[0].index("--cookies") + 1] == str(
        cookie_file.resolve()
    )


def test_thumbnail_download_keeps_native_format_without_ffmpeg(
    tmp_path, monkeypatch,
):
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.webp").write_bytes(b"webp")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_subtitles=False,
        download_thumbnail=True,
    )

    assert "--convert-thumbnails" not in commands[0]
    assert artifacts["thumbnail"].endswith("abcdefghijk.webp")


def test_portable_download_uses_bundled_yt_dlp_module(tmp_path, monkeypatch):
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.jpg").write_bytes(b"jpg")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setenv("SUBTITLE_PORTABLE", "1")
    monkeypatch.setattr(
        "web.downloads.shutil.which",
        lambda name: r"C:\untrusted\yt-dlp.exe" if name == "yt-dlp" else None,
    )
    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_subtitles=False,
        download_thumbnail=True,
    )

    assert commands[0][:3] == [sys.executable, "-m", "yt_dlp"]
    assert r"C:\untrusted\yt-dlp.exe" not in commands[0]


def test_download_child_env_prepends_project_runtime_deps(tmp_path, monkeypatch):
    """The download child must resolve yt_dlp like the launcher environment.

    start_web.bat prefers the bundled runtime + PYTHONPATH=.runtime_deps, where
    yt_dlp is not in site-packages.  When the project's .runtime_deps exists it
    is prepended (deduplicated); otherwise the inherited environment is kept.
    """
    import os

    import web.downloads as downloads

    deps = tmp_path / "runtime_deps"
    deps.mkdir()
    monkeypatch.setattr(downloads, "_project_runtime_deps_dir", lambda: deps)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["existing", "other"]))
    env = downloads._download_child_env()
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == str(deps)
    assert "existing" in entries and "other" in entries
    assert env["PYTHONPATH"].count("existing") == 1

    # 无 runtime_deps 时继承原环境不变
    monkeypatch.setattr(
        downloads, "_project_runtime_deps_dir",
        lambda: tmp_path / "missing",
    )
    env = downloads._download_child_env()
    assert env["PYTHONPATH"] == os.pathsep.join(["existing", "other"])


def test_download_missing_yt_dlp_module_reports_root_cause(tmp_path, monkeypatch):
    """Missing-module startup failure must not be swallowed into a generic error."""
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 1

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO(
                "Traceback (most recent call last):\n"
                "  File \"<string>\", line 1, in <module>\n"
                "ModuleNotFoundError: No module named 'yt_dlp'\n"
            )
            output.mkdir(exist_ok=True)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    with pytest.raises(RuntimeError) as excinfo:
        run_download(
            "https://youtu.be/abcdefghijk",
            str(output),
            download_video=False,
            download_subtitles=False,
            download_thumbnail=True,
        )

    message = str(excinfo.value)
    assert "缺少 yt_dlp" in message
    assert "install_web.bat" in message
    assert "C:" not in message


def test_download_can_read_selected_browser_cookies(tmp_path, monkeypatch):
    output = tmp_path / "output"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            self.stdout = io.StringIO("")
            output.mkdir(exist_ok=True)
            (output / "abcdefghijk.jpg").write_bytes(b"jpg")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_video=False,
        download_subtitles=False,
        download_thumbnail=True,
        cookies_from_browser="chrome",
    )

    assert "--cookies-from-browser" in commands[0]
    assert commands[0][commands[0].index("--cookies-from-browser") + 1] == "chrome"


def test_bot_check_failure_returns_actionable_message(tmp_path, monkeypatch):
    class Process:
        returncode = 1

        def __init__(self, command, **options):
            self.stdout = io.StringIO(
                "WARNING: HTTP Error 429: Too Many Requests\n"
                "ERROR: Sign in to confirm you’re not a bot. "
                "Use --cookies-from-browser or --cookies\n"
            )

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    with pytest.raises(RuntimeError) as raised:
        run_download(
            "https://youtu.be/abcdefghijk",
            str(tmp_path / "output"),
            download_video=False,
            download_subtitles=False,
            download_thumbnail=True,
        )

    assert "YouTube 已触发限流或机器人验证" in str(raised.value)
    assert "Google Chrome" in str(raised.value)
    assert "cookies.txt" in str(raised.value)


def test_locked_browser_cookie_store_returns_actionable_message(
        tmp_path, monkeypatch):
    class Process:
        returncode = 1

        def __init__(self, command, **options):
            self.stdout = io.StringIO(
                "ERROR: Could not copy Chrome cookie database. "
                "See issue https://github.com/yt-dlp/yt-dlp/issues/7271\n"
            )

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr("web.downloads.subprocess.Popen", Process)

    with pytest.raises(RuntimeError) as raised:
        run_download(
            "https://youtu.be/abcdefghijk",
            str(tmp_path / "output"),
            download_video=False,
            download_subtitles=False,
            download_thumbnail=True,
            cookies_from_browser="chrome",
        )

    assert "无法读取 Google Chrome Cookie" in str(raised.value)
    assert "完全退出 Chrome" in str(raised.value)
    assert "cookies.txt" in str(raised.value)


# --- 下载韧性：分类 / 摘要 / 降级阶梯 ------------------------------------

class _FakeProcess:
    """Popen stand-in replaying one canned outcome per invocation."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append(command)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        self.returncode = outcome.get("returncode", 0)
        for name in outcome.get("files", ()):
            Path(options["cwd"], name).write_bytes(b"payload" * 8)
        self.stdout = io.StringIO(outcome.get("stdout", ""))
        return self

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.parametrize("text, expected", [
    ("ModuleNotFoundError: No module named 'yt_dlp'", "yt_dlp_missing"),
    ("ERROR: Could not copy Chrome cookie database", "cookie_decrypt"),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limit"),
    ("ERROR: Sign in to confirm you're not a bot", "bot_challenge"),
    ("ERROR: HTTP Error 403", "bot_challenge"),
    ("ERROR: Signature extraction failed", "bot_challenge"),
    ("ERROR: The page needs to be reloaded.", "session_reload"),
    ("ERROR: Requested format is not available", "format"),
    ("ERROR: Only images are available", "format"),
    ("ERROR: 'NoneType' object has no attribute 'lower'", "merge_crash"),
    ("ERROR: something entirely different", "unknown"),
])
def test_download_failure_is_classified(text, expected):
    assert _classify_download_failure(text) == expected


def test_missing_module_is_not_misread_as_bot_challenge():
    """启动期故障必须优先于风控判定，否则根因会被吞掉。"""
    tail = (
        "ERROR: HTTP Error 403\n"
        "ModuleNotFoundError: No module named 'yt_dlp'\n"
    )
    assert _classify_download_failure(tail) == "yt_dlp_missing"


def test_summary_prefers_last_error_line():
    lines = [
        "[download]  12.0% of 100MiB",
        "ERROR: first failure",
        "[download]  13.0% of 100MiB",
        "ERROR: Requested format is not available",
    ]
    assert _summarize_yt_dlp_error(lines) == (
        "ERROR: Requested format is not available"
    )


def test_summary_redacts_filesystem_paths_but_keeps_urls():
    # 夹具刻意用虚构盘符与虚构目录：仓库的隐私扫描（tests/
    # test_secret_hygiene.py）会把形如个人主目录的路径判为泄露，
    # 连注释里出现的字面量也会命中。
    lines = [
        "ERROR: unable to write "
        r"D:\FixtureHome\alice\secret\clip.srt and /srv/media/clip.srt"
    ]
    summary = _summarize_yt_dlp_error(lines)
    assert "alice" not in summary
    assert summary.count("<路径已省略>") == 2

    url_line = (
        "ERROR: unable to download "
        "https://rr1---sn-abc.googlevideo.com/videoplayback?id=1"
    )
    url_summary = _summarize_yt_dlp_error([url_line])
    assert "https://rr1---sn-abc.googlevideo.com/videoplayback?id=1" in url_summary


def test_quality_strategies_degrade_to_a_merge_free_single_stream():
    strategies = _build_quality_strategies("1080")
    assert len(strategies) == 2
    assert strategies[0]["format"] == (
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    )
    assert strategies[0]["merge_output_format"] == "mp4"
    assert strategies[1]["merge_output_format"] is None
    assert "+" not in strategies[1]["format"]
    # 降级后仍要求带音轨：本地 Whisper 需要音频，无声视频没有意义。
    assert "acodec!=none" in strategies[1]["format"]


def test_quality_strategies_for_best_quality_have_no_height_cap():
    strategies = _build_quality_strategies("best")
    assert strategies[0]["format"] == "bestvideo+bestaudio/best"
    assert strategies[1]["format"] == "best[acodec!=none]/best"


def test_video_download_degrades_when_format_is_unavailable(tmp_path, monkeypatch):
    output = tmp_path / "output"
    fake = _FakeProcess([
        {"returncode": 1, "stdout": "ERROR: Requested format is not available\n"},
        {"returncode": 0, "files": ["abcdefghijk.mp4"]},
    ])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)
    messages = []

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
        emit=messages.append,
    )

    assert len(fake.calls) == 2
    assert "--merge-output-format" in fake.calls[0]
    assert "--merge-output-format" not in fake.calls[1]
    assert any("降级" in message for message in messages)
    assert Path(artifacts["video"]).name == "abcdefghijk.mp4"


def test_video_download_retries_anonymously_on_session_mismatch(
        tmp_path, monkeypatch):
    cookie = tmp_path / "youtube.cookies.txt"
    cookie.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    fake = _FakeProcess([
        {"returncode": 1, "stdout": "ERROR: The page needs to be reloaded.\n"},
        {"returncode": 0, "files": ["abcdefghijk.mp4"]},
    ])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)
    messages = []

    run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
        cookies_file=str(cookie),
        emit=messages.append,
    )

    assert "--cookies" in fake.calls[0]
    assert "--cookies" not in fake.calls[1]
    assert any("匿名" in message for message in messages)
    # 只改内存副本，用户磁盘上的 Cookie 文件不受影响。
    assert cookie.is_file()


def test_video_download_merges_with_ffmpeg_when_yt_dlp_merge_crashes(
        tmp_path, monkeypatch):
    output = tmp_path / "output"
    fake = _FakeProcess([{
        "returncode": 1,
        "stdout": "ERROR: 'NoneType' object has no attribute 'lower'\n",
        "files": ["abcdefghijk.f616.mp4", "abcdefghijk.f251.m4a"],
    }])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)
    monkeypatch.setattr(
        "web.downloads.shutil.which",
        lambda name: "ffmpeg" if name == "ffmpeg" else None,
    )
    ffmpeg_calls = []

    class Completed:
        returncode = 0
        stdout = ""

    def fake_ffmpeg(command, **options):
        ffmpeg_calls.append(command)
        Path(command[-1]).write_bytes(b"merged-video")
        return Completed()

    monkeypatch.setattr("web.downloads.subprocess.run", fake_ffmpeg)
    messages = []

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
        emit=messages.append,
    )

    assert len(fake.calls) == 1
    assert len(ffmpeg_calls) == 1
    assert "-c" in ffmpeg_calls[0] and "copy" in ffmpeg_calls[0]
    assert any("ffmpeg" in message for message in messages)
    assert Path(artifacts["video"]).name == "abcdefghijk.mp4"
    # 合并成功后分片必须清理：留着半成品会污染任务目录，
    # 而且按后缀选片时会被误当成成品。
    assert not list(output.glob("*.f616.mp4"))
    assert not list(output.glob("*.f251.m4a"))


def test_video_download_caps_attempts_and_reports_summary(tmp_path, monkeypatch):
    cookie = tmp_path / "youtube.cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    output = tmp_path / "output"
    fake = _FakeProcess([
        {"returncode": 1, "stdout": "ERROR: The page needs to be reloaded.\n"},
        {"returncode": 1, "stdout": "ERROR: Requested format is not available\n"},
        {"returncode": 1, "stdout": "ERROR: Requested format is not available\n"},
        {"returncode": 1, "stdout": "ERROR: should never be reached\n"},
    ])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)

    with pytest.raises(RuntimeError) as raised:
        run_download(
            "https://youtu.be/abcdefghijk",
            str(output),
            download_subtitles=False,
            download_thumbnail=False,
            cookies_file=str(cookie),
        )

    assert len(fake.calls) == 3
    assert "Requested format is not available" in str(raised.value)


def test_video_failure_message_never_leaks_local_paths(tmp_path, monkeypatch):
    fake = _FakeProcess([{
        "returncode": 1,
        "stdout":
            r"ERROR: failed writing D:\FixtureHome\alice\secret\clip.mp4" + "\n",
    }])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)

    with pytest.raises(RuntimeError) as raised:
        run_download(
            "https://youtu.be/abcdefghijk",
            str(tmp_path / "output"),
            download_subtitles=False,
            download_thumbnail=False,
        )

    message = str(raised.value)
    assert "alice" not in message
    assert "<路径已省略>" in message


def test_download_declares_the_local_js_runtime(tmp_path, monkeypatch):
    output = tmp_path / "output"
    fake = _FakeProcess([{"returncode": 0, "files": ["abcdefghijk.mp4"]}])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)
    monkeypatch.setattr(
        "web.downloads.shutil.which",
        lambda name: "/opt/bin/deno" if name == "deno" else None,
    )

    run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
    )

    command = fake.calls[0]
    assert "--js-runtimes" in command
    assert command[command.index("--js-runtimes") + 1] == "deno"


def test_download_accepts_video_containers_other_than_mp4(tmp_path, monkeypatch):
    """降级路径可能产出 webm/mkv，不能因为不是 .mp4 就判定下载失败。"""
    output = tmp_path / "output"
    fake = _FakeProcess([
        {"returncode": 1, "stdout": "ERROR: Requested format is not available\n"},
        {"returncode": 0, "files": ["abcdefghijk.webm"]},
    ])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)

    artifacts = run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
    )

    assert Path(artifacts["video"]).name == "abcdefghijk.webm"


def test_resilience_args_default_to_timeouts_and_fragment_concurrency():
    """挂起连接必须自己超时，否则单 worker 串行队列会被永久堵住。"""
    args = _build_resilience_args()

    assert args[args.index("--socket-timeout") + 1] == "30"
    assert args[args.index("--retry-sleep") + 1] == "3"
    assert args[args.index("--concurrent-fragments") + 1] == "4"


def test_resilience_args_leave_request_fingerprint_untouched_by_default():
    """UA 与 geo-bypass 会改变请求指纹，只有 env 显式打开才拼进命令。"""
    args = _build_resilience_args()

    assert "--user-agent" not in args
    assert "--geo-bypass" not in args


def test_resilience_args_honour_explicit_fingerprint_switches(monkeypatch):
    monkeypatch.setattr(Config, "YTDLP_USER_AGENT", "Mozilla/5.0 (TestUA)")
    monkeypatch.setattr(Config, "YTDLP_GEO_BYPASS", True)

    args = _build_resilience_args()

    assert args[args.index("--user-agent") + 1] == "Mozilla/5.0 (TestUA)"
    assert "--geo-bypass" in args


def test_resilience_args_can_be_disabled_entirely(monkeypatch):
    monkeypatch.setattr(Config, "YTDLP_SOCKET_TIMEOUT", 0.0)
    monkeypatch.setattr(Config, "YTDLP_RETRY_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(Config, "YTDLP_CONCURRENT_FRAGMENTS", 1)

    assert _build_resilience_args() == []


def test_download_command_carries_socket_timeout(tmp_path, monkeypatch):
    output = tmp_path / "output"
    fake = _FakeProcess([{"returncode": 0, "files": ["abcdefghijk.mp4"]}])
    monkeypatch.setattr("web.downloads.subprocess.Popen", fake)

    run_download(
        "https://youtu.be/abcdefghijk",
        str(output),
        download_subtitles=False,
        download_thumbnail=False,
    )

    command = fake.calls[0]
    assert command[command.index("--socket-timeout") + 1] == "30"
    assert command[command.index("--retry-sleep") + 1] == "3"
    assert command[command.index("--concurrent-fragments") + 1] == "4"
