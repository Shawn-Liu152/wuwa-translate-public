"""Download YouTube media and auto English subtitles for a web job."""
from __future__ import annotations

import os
import html
import json
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from pipeline.config import Config
from pipeline.pipeline_manifest import PipelineCancelled
from pipeline.languages import normalize_language_pair

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}
VIDEO_QUALITY_HEIGHTS = {
    "best": None,
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
}
BROWSER_COOKIE_SOURCES = {"chrome"}
_PROGRESS_MARKER = "SUBTITLE_PROGRESS "
_VTT_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d+:)?\d{2}:\d{2}\.\d{3})(?:\s+.*)?$"
)
_VTT_INLINE_TIMESTAMP_RE = re.compile(
    r"<(?:\d+:)?\d{2}:\d{2}\.\d{3}>"
)
_VTT_RUBY_TEXT_RE = re.compile(r"<rt(?:\s[^>]*)?>.*?</rt>", re.IGNORECASE)
_VTT_TAG_RE = re.compile(r"</?[^>]+>")

# --- 下载失败分类 ----------------------------------------------------------
# 下载失败先分类、再按类别决定动作（画质降级 / 改用匿名 / ffmpeg 兜底合并），
# 而不是把 yt-dlp 的原始输出原样抛给用户。
FAILURE_YT_DLP_MISSING = "yt_dlp_missing"
FAILURE_COOKIE_DECRYPT = "cookie_decrypt"
FAILURE_RATE_LIMIT = "rate_limit"
FAILURE_BOT_CHALLENGE = "bot_challenge"
FAILURE_SESSION_RELOAD = "session_reload"
FAILURE_FORMAT = "format"
FAILURE_MERGE_CRASH = "merge_crash"
FAILURE_UNKNOWN = "unknown"

_SUMMARY_MAX_CHARS = 300
_PATH_REDACTED = "<路径已省略>"
# 只匹配「像文件系统路径」的片段，刻意不误伤 URL：
# (?<![\w:/]) 保证 http:// 之类不会被当成盘符路径或 POSIX 路径。
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/])[A-Za-z]:[\\/][^\s\"'|<>]*")
_POSIX_PATH_RE = re.compile(r"(?<![\w:/])/(?:[^\s\"'|<>]+/)+[^\s\"'|<>]*")

_VIDEO_FILE_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv"}
_AUDIO_FILE_SUFFIXES = {".m4a", ".opus", ".mp3", ".aac", ".ogg", ".flac"}
# yt-dlp 未合并完的残留分片形如 <id>.f616.mp4 / <id>.f251.m4a。
_FRAGMENT_RE = re.compile(r"\.f\d+\.", re.IGNORECASE)


def _redact_paths(text: str) -> str:
    """Strip filesystem paths from a message destined for the job log."""
    text = _WINDOWS_PATH_RE.sub(_PATH_REDACTED, text)
    return _POSIX_PATH_RE.sub(_PATH_REDACTED, text)


def _summarize_yt_dlp_error(lines: list[str]) -> str:
    """Return one redacted, human-readable line describing the failure.

    yt-dlp 失败时往往吐出几十行进度与警告，全部回显会把真正的报错淹没掉。
    这里只取最有价值的一行：最后一条 ERROR:，其次最后一条 [youtube]/[download]。
    """
    candidates: list[str] = []
    for raw in lines:
        line = str(raw).strip()
        if not line:
            continue
        if line.startswith("ERROR:"):
            candidates.append(line)
        elif "[youtube]" in line or "[download]" in line:
            candidates.append(line)
    if candidates:
        summary = candidates[-1]
    else:
        non_empty = [str(raw).strip() for raw in lines if str(raw).strip()]
        summary = non_empty[-1] if non_empty else "未知错误"
    return _redact_paths(summary)[:_SUMMARY_MAX_CHARS]


def _classify_download_failure(text: str) -> str:
    """Map a yt-dlp output tail to a failure kind.

    判定顺序刻意把「启动期故障」（模块缺失、Cookie 解密失败）排在风控之前，
    否则一条 ModuleNotFoundError 也会被里面的 403/bot 关键字误伤。
    """
    lowered = str(text).casefold()
    if "no module named" in lowered and "yt_dlp" in lowered:
        return FAILURE_YT_DLP_MISSING
    if any(marker in lowered for marker in (
        "cookie database", "failed to decrypt", "could not decrypt",
    )):
        return FAILURE_COOKIE_DECRYPT
    if any(marker in lowered for marker in ("http error 429", "too many requests")):
        return FAILURE_RATE_LIMIT
    if any(marker in lowered for marker in (
        "sign in to confirm", "not a bot", "signature extraction failed",
        "http error 403", "decodeuricomponent",
    )):
        return FAILURE_BOT_CHALLENGE
    if "the page needs to be reloaded" in lowered:
        return FAILURE_SESSION_RELOAD
    if any(marker in lowered for marker in (
        "requested format is not available", "only images are available",
    )):
        return FAILURE_FORMAT
    if "'nonetype' object has no attribute 'lower'" in lowered:
        return FAILURE_MERGE_CRASH
    return FAILURE_UNKNOWN


def _build_quality_strategies(video_quality: str) -> list[dict]:
    """Return download strategies, best quality first.

    第二级刻意用单一流并去掉 --merge-output-format：合并阶段是 yt-dlp 最
    常见的硬失败点，单一流完全不经过合并。``[acodec!=none]`` 保证降级后
    仍然拿到带音轨的文件 —— 本地 Whisper 需要音频，无声视频没有意义。
    """
    height = VIDEO_QUALITY_HEIGHTS[video_quality]
    if height is None:
        fallback_format = "best[acodec!=none]/best"
    else:
        fallback_format = (
            f"best[height<={height}][acodec!=none]"
            f"/best[height<={height}]/best"
        )
    return [
        {
            "label": "最高画质（音视频合并）",
            "format": video_format_selector(video_quality),
            "merge_output_format": "mp4",
        },
        {
            "label": "单一流（无需合并，画质可能下降）",
            "format": fallback_format,
            "merge_output_format": None,
        },
    ]


def _build_js_runtime_args() -> list[str]:
    """Declare the local JS runtime yt-dlp should use to solve challenges.

    只声明 runtime，不显式打开 --remote-components ejs:github：那会让
    yt-dlp 去 GitHub 拉组件，在国内网络下凭空多一个不稳定依赖；而默认
    行为已经足够（本机已验证匿名全自动下载可用）。
    """
    for runtime in ("deno", "node"):
        if shutil.which(runtime):
            return ["--js-runtimes", runtime]
    return []


def _build_resilience_args() -> list[str]:
    """Return yt-dlp network-resilience flags from ``Config``.

    单 worker 串行下载时，一条挂起的连接会堵死整条队列；yt-dlp 默认不设
    socket 超时，所以这里显式声明。UA 与 geo-bypass 会改变请求指纹，
    默认关闭，只有 env 显式打开才拼进命令。
    """
    args: list[str] = []
    if Config.YTDLP_SOCKET_TIMEOUT > 0:
        args += ["--socket-timeout", f"{Config.YTDLP_SOCKET_TIMEOUT:g}"]
    if Config.YTDLP_RETRY_SLEEP_SECONDS > 0:
        args += ["--retry-sleep", f"{Config.YTDLP_RETRY_SLEEP_SECONDS:g}"]
    if Config.YTDLP_CONCURRENT_FRAGMENTS > 1:
        args += ["--concurrent-fragments", str(Config.YTDLP_CONCURRENT_FRAGMENTS)]
    if Config.YTDLP_USER_AGENT:
        args += ["--user-agent", Config.YTDLP_USER_AGENT]
    if Config.YTDLP_GEO_BYPASS:
        args.append("--geo-bypass")
    return args


def _pick_video_file(output_dir: Path, video_id: str) -> Path | None:
    """Locate the produced video file regardless of container."""
    matches = [
        path for path in output_dir.glob(f"{video_id}.*")
        if path.is_file()
        and path.stat().st_size > 0
        and path.suffix.casefold() in _VIDEO_FILE_SUFFIXES
    ]
    if not matches:
        return None

    def priority(path: Path) -> tuple[int, int, int, str]:
        name = path.name.casefold()
        return (
            # 规范名 <id>.mp4 优先：ffmpeg 兜底合并的产物就是它。
            0 if name == f"{video_id.casefold()}.mp4" else 1,
            # 其次排除未合并完的残留分片，避免把只有画面的 fXXX 当成成品。
            1 if _FRAGMENT_RE.search(path.name) else 0,
            0 if path.suffix.casefold() == ".mp4" else 1,
            name,
        )

    return min(matches, key=priority)


def _merge_with_ffmpeg(output_dir: Path, video_id: str) -> bool:
    """Re-mux / merge yt-dlp leftovers when its own merge step crashed.

    yt-dlp 合并崩溃时通常留下 ``<id>.fXXX.mp4`` 与 ``<id>.fXXX.m4a`` 分片。
    这里只做 ``-c copy``，不转码，也不去猜分片编号 —— 按后缀挑候选即可。
    找不到候选、或本机没有 ffmpeg 时返回 False，交给上层按普通失败处理。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    candidates = [
        path for path in output_dir.glob(f"{video_id}.*")
        if path.is_file() and path.stat().st_size > 0
    ]
    video_sources = sorted(
        path for path in candidates
        if path.suffix.casefold() in _VIDEO_FILE_SUFFIXES
    )
    audio_sources = sorted(
        path for path in candidates
        if path.suffix.casefold() in _AUDIO_FILE_SUFFIXES
    )
    target = output_dir / f"{video_id}.mp4"
    video_sources = [
        path for path in video_sources if path.resolve() != target.resolve()
    ]
    if not video_sources:
        return False
    for source in video_sources:
        command = [ffmpeg, "-y", "-i", str(source)]
        if audio_sources:
            command += ["-i", str(audio_sources[0])]
        command += ["-c", "copy", "-f", "mp4", str(target)]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if (
            completed.returncode == 0
            and target.is_file()
            and target.stat().st_size > 0
        ):
            # 合并成功，清掉被消费掉的分片，避免任务目录里留下半成品，
            # 也避免后续按后缀选片时误选只有画面/只有声音的残留文件。
            for leftover in video_sources + audio_sources:
                if not _FRAGMENT_RE.search(leftover.name):
                    continue
                try:
                    leftover.unlink()
                except OSError:
                    pass
            return True
    return False


def _language_label(source_language: str) -> str:
    return {"en": "英语", "ja": "日语", "ko": "韩语"}.get(source_language, source_language)


def _project_runtime_deps_dir() -> Path:
    """Return the project root's ``.runtime_deps`` directory, if any."""
    return Path(__file__).resolve().parents[1] / ".runtime_deps"


def _download_child_env() -> dict[str, str]:
    """Return the child environment used to run yt-dlp.

    start_web.bat runs the bundled runtime with ``PYTHONPATH=.runtime_deps``
    (yt-dlp and friends live there, not in the runtime's site-packages).  The
    download subprocess must use the same dependency layout so the module
    resolves the way the Web process itself was launched.  When ``.runtime_deps``
    exists it is prepended to PYTHONPATH (deduplicated); otherwise the inherited
    environment is kept unchanged so a venv/site-packages launch behaves as
    before.
    """
    env = dict(os.environ)
    runtime_deps = _project_runtime_deps_dir()
    if not runtime_deps.is_dir():
        return env
    existing = env.get("PYTHONPATH", "").split(os.pathsep)
    entries: list[str] = [str(runtime_deps)]
    seen = {str(runtime_deps)}
    for entry in existing:
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _display_download_line(line: str) -> str:
    """Localise the ASCII-only yt-dlp progress marker in the parent process."""
    if line.startswith(_PROGRESS_MARKER):
        return "下载进度 " + line[len(_PROGRESS_MARKER):]
    return line


def _vtt_timestamp_to_srt(value: str) -> str:
    """Convert a WebVTT timestamp to the strict timestamp used by our SRT parser."""
    parts = value.split(":")
    if len(parts) == 2:
        parts.insert(0, "00")
    if len(parts) != 3 or len(parts[0]) > 2:
        raise ValueError(f"不支持的 WebVTT 时间戳: {value}")
    timestamp = ":".join((parts[0].zfill(2), parts[1], parts[2])).replace(
        ".", ","
    )
    from pipeline.parser.srt_parser import timestamp_to_ms

    timestamp_to_ms(timestamp)
    return timestamp


def _clean_vtt_payload_line(value: str) -> str:
    value = _VTT_INLINE_TIMESTAMP_RE.sub("", value)
    value = _VTT_RUBY_TEXT_RE.sub("", value)
    value = _VTT_TAG_RE.sub("", value)
    return html.unescape(value).replace("\u00a0", " ").replace("\u200b", "").strip()


def convert_webvtt_to_srt(source: Path, destination: Path) -> Path:
    """Convert a YouTube WebVTT subtitle using only the Python standard library."""
    from pipeline.parser.srt_parser import Subtitle, save_srt

    raw = source.read_text(encoding="utf-8-sig")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    subtitles: list[Subtitle] = []
    for block in re.split(r"\n[ \t]*\n", raw.strip()):
        lines = [line.rstrip() for line in block.split("\n")]
        if not lines or lines[0].lstrip().startswith("WEBVTT"):
            continue
        if lines[0].strip().upper() in {"STYLE", "REGION"}:
            continue
        if lines[0].lstrip().upper().startswith("NOTE"):
            continue
        timing_index = next(
            (index for index, line in enumerate(lines[:2]) if _VTT_TIMING_RE.match(line)),
            None,
        )
        if timing_index is None:
            continue
        timing = _VTT_TIMING_RE.match(lines[timing_index])
        assert timing is not None
        payload_lines: list[str] = []
        for line in lines[timing_index + 1:]:
            cleaned = _clean_vtt_payload_line(line)
            if cleaned and (not payload_lines or cleaned != payload_lines[-1]):
                payload_lines.append(cleaned)
        payload = "\n".join(payload_lines)
        if not payload:
            continue
        subtitles.append(Subtitle(
            id=len(subtitles) + 1,
            start=_vtt_timestamp_to_srt(timing.group("start")),
            end=_vtt_timestamp_to_srt(timing.group("end")),
            text=payload,
        ))
    if not subtitles:
        raise ValueError("WebVTT 字幕中没有可转换的有效字幕")
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_srt(str(destination), subtitles)
    return destination


def prepare_youtube_subtitles(
    source: Path, destination: Path, source_language: str = "en",
) -> Path:
    """Create a cleaned, de-rolled translation source without changing raw SRT."""
    from pipeline.parser.srt_parser import parse_srt, save_srt
    from pipeline.preprocess.dedupe import dedupe_youtube_subs

    subtitles = dedupe_youtube_subs(
        parse_srt(str(source)), source_language=source_language,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_srt(str(destination), subtitles)
    return destination


def video_format_selector(video_quality: str) -> str:
    quality = str(video_quality).strip().lower()
    if quality not in VIDEO_QUALITY_HEIGHTS:
        raise ValueError("无效的视频清晰度")
    max_height = VIDEO_QUALITY_HEIGHTS[quality]
    if max_height is None:
        return "bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={max_height}]+bestaudio/"
        f"best[height<={max_height}]"
    )


def extract_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise ValueError("请输入有效的 YouTube 视频链接")
    parts = [part for part in parsed.path.split("/") if part]
    video_id = ""
    if host.endswith("youtu.be"):
        video_id = parts[0] if parts else ""
    elif parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif parts and parts[0] in {"shorts", "live", "embed"}:
        video_id = parts[1] if len(parts) > 1 else ""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("请输入有效的 YouTube 视频链接")
    return video_id


def _subtitle_language_selector(source_language: str) -> str:
    """Return yt-dlp language patterns for the declared source language."""

    if source_language == "ja":
        # yt-dlp accepts comma-separated language expressions.  Request the
        # exact track first, then the common regional and generic ja-* forms.
        return "ja,ja-JP,ja.*"
    if source_language == "ko":
        return "ko,ko-KR,ko.*"
    return source_language


def _pick_source_subtitle(
    output: Path, video_id: str, source_language: str,
) -> Path | None:
    """Choose one declared-language SRT/VTT without treating variants as English.

    An exact ``.ja.srt`` track is the normal manually supplied YouTube track.
    Regional tracks come next; files explicitly labelled as original/automatic
    fall behind them.  The remaining sort key makes selection deterministic.
    """

    matches = [
        path for path in output.glob(f"{video_id}*")
        if path.suffix.casefold() in {".srt", ".vtt"}
        if re.search(
            rf"\.{re.escape(source_language)}(?:[-_.]|\.(?:srt|vtt))",
            path.name,
            re.IGNORECASE,
        )
    ]
    if not matches:
        return None

    exact_suffixes = (
        f".{source_language}.srt",
        f".{source_language}.vtt",
    )
    regional_prefix = f".{source_language}-"

    def priority(path: Path) -> tuple[int, int, str]:
        name = path.name.casefold()
        extension_priority = 0 if path.suffix.casefold() == ".srt" else 1
        if any(name.endswith(suffix.casefold()) for suffix in exact_suffixes):
            return 0, extension_priority, name
        if name.startswith(video_id.casefold()) and regional_prefix in name:
            if any(marker in name for marker in ("-orig", "-auto", "-asr")):
                return 2, extension_priority, name
            return 1, extension_priority, name
        return 3, extension_priority, name

    return min(matches, key=priority)


def _dedupe_subtitle_variants(
    output: Path, video_id: str, source_language: str, keep: Path,
) -> None:
    """Remove redundant same-language subtitle variants left by yt-dlp.

    ``--write-subs --write-auto-subs`` writes both the manually uploaded track
    (``<id>.<lang>.srt``) and the auto-generated original track
    (``<id>.<lang>-orig.srt``), which are often byte-identical.  Keep only the
    selected track plus any ``.cleaned``/``.aligned`` files; delete the rest.
    """
    keep_resolved = keep.resolve()
    for path in output.glob(f"{video_id}*"):
        if path.suffix.casefold() not in {".srt", ".vtt"}:
            continue
        if path.resolve() == keep_resolved:
            continue
        if ".cleaned" in path.name or ".aligned" in path.name:
            continue
        if re.search(
            rf"\.{re.escape(source_language)}(?:[-_.]|\.(?:srt|vtt))",
            path.name,
            re.IGNORECASE,
        ):
            try:
                path.unlink()
            except OSError:
                pass


def _pick_thumbnail(output: Path, video_id: str) -> Path | None:
    """Return a browser-displayable native thumbnail without media conversion."""
    priorities = {
        ".jpg": 0,
        ".jpeg": 1,
        ".png": 2,
        ".webp": 3,
        ".avif": 4,
        ".gif": 5,
    }
    matches = [
        path for path in output.glob(f"{video_id}.*")
        if path.is_file()
        and path.stat().st_size > 0
        and path.suffix.casefold() in priorities
    ]
    return min(
        matches,
        key=lambda path: (priorities[path.suffix.casefold()], path.name.casefold()),
        default=None,
    )


def run_download(
    url: str,
    output_dir: str,
    proxy: str = "",
    download_video: bool = True,
    download_subtitles: bool = True,
    download_thumbnail: bool = True,
    video_quality: str = "1080",
    allow_missing_subtitles: bool = False,
    cookies_file: str = "",
    cookies_from_browser: str = "",
    emit: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    source_language: str = "en",
) -> dict[str, str]:
    """Run yt-dlp in small explicit stages and return only verified artifacts.

    Output is streamed line-by-line so the caller sees progress in real time.
    If *cancel_check* returns True at any point, the subprocess is killed and
    a RuntimeError is raised.
    """
    video_id = extract_video_id(url)
    source_language = normalize_language_pair({
        "source_language": source_language,
    })["source_language"]
    video_quality = str(video_quality).strip().lower()
    # 校验清晰度取值（非法值在此抛出）；实际 format 由下载策略内部生成。
    video_format_selector(video_quality)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # APP-SUPPLY-001: always use the audited environment's module.  A PATH
    # executable must never receive the path of a private Cookie file.
    #
    # The child must run with the same module layout as the Web launcher
    # (start_web.bat prefers the bundled runtime with PYTHONPATH=.runtime_deps,
    # where yt_dlp lives instead of in site-packages).  `-I` isolation would
    # strip PYTHONPATH and make every download fail there, so we invoke
    # `python -m yt_dlp` without isolation and prepend `.runtime_deps` to the
    # child's PYTHONPATH when present (see _download_child_env).
    base = [sys.executable, "-m", "yt_dlp"]

    prefix = base + [
        "--newline",
        "--retries", "10",
        "--fragment-retries", "10",
        "--progress-template",
        "download:SUBTITLE_PROGRESS %(progress._percent_str)s | "
        "%(progress._speed_str)s | ETA %(progress._eta_str)s",
    ]
    # 网络弹性参数（socket 超时/重试间隔/分片并发）：防止一条挂起的连接
    # 堵死单 worker 串行队列。取值全部来自 env，见 Config.YTDLP_*。
    prefix += _build_resilience_args()
    # 显式声明本机 JS runtime，让 yt-dlp 解 n/challenge 时不必回退猜测。
    prefix += _build_js_runtime_args()
    if proxy.strip():
        prefix += ["--proxy", proxy.strip()]
    if cookies_file.strip():
        cookie_path = Path(cookies_file).expanduser().resolve()
        if not cookie_path.is_file():
            raise ValueError("YouTube Cookie 文件不存在")
        prefix += ["--cookies", str(cookie_path)]
    browser = cookies_from_browser.strip().lower()
    if browser:
        if cookies_file.strip():
            raise ValueError("Cookie 文件和浏览器 Cookie 只能选择一种")
        if browser not in BROWSER_COOKIE_SOURCES:
            raise ValueError("不支持的浏览器 Cookie 来源")
        prefix += ["--cookies-from-browser", browser]
    authentication_enabled = bool(cookies_file.strip() or browser)

    artifacts: dict[str, str] = {}

    def _run_capture(
        args: list[str], label: str, command_prefix: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Run yt-dlp, streaming output through *emit*, without raising.

        Returns ``(succeeded, collected_lines)`` so the video stage can retry
        with a different strategy instead of losing the diagnostic output.
        """
        if cancel_check and cancel_check():
            raise PipelineCancelled("任务已取消")
        if emit:
            emit(label)

        popen_options = {
            "cwd": output,
            "env": _download_child_env(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(
            (command_prefix or prefix) + args, **popen_options)
        last_lines: list[str] = []
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for raw_line in process.stdout:
                    lines.put(raw_line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def stop_process() -> None:
            if process.poll() is not None:
                return
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()

        while True:
            if cancel_check and cancel_check():
                stop_process()
                raise PipelineCancelled("任务已取消")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                break
            line = _display_download_line(line.strip())
            if not line:
                continue
            if emit:
                emit(line)
            last_lines.append(line)
            if len(last_lines) > 20:
                last_lines.pop(0)

        process.wait()
        reader.join(timeout=1)
        return (not process.returncode, last_lines)

    def _raise_failure(label: str, lines: list[str]) -> None:
        """Classify collected output and raise the matching user-facing error."""
        kind = _classify_download_failure("\n".join(lines[-10:]))
        if kind == FAILURE_YT_DLP_MISSING:
            # 下载组件缺失是环境问题，必须保留根因而非吞成通用错误。
            # 消息为本地生成、不含路径，符合安全边界。
            raise RuntimeError(
                "下载组件未就绪：当前运行环境缺少 yt_dlp 模块，"
                "请先运行 install_web.bat 安装依赖后重启工作台"
            )
        if browser and kind == FAILURE_COOKIE_DECRYPT:
            raise RuntimeError(
                "无法读取 Google Chrome Cookie。请完全退出 Chrome（包括"
                "后台进程）后重试；若仍失败，请直接粘贴 cookies.txt 内容"
            )
        if kind in (FAILURE_RATE_LIMIT, FAILURE_BOT_CHALLENGE):
            if authentication_enabled:
                raise RuntimeError(
                    "YouTube 仍拒绝当前 Cookie。请先在所选浏览器登录 "
                    "YouTube；若 Chrome 读取失败，请关闭浏览器后重试，"
                    "或改为粘贴最新的 cookies.txt 内容"
                )
            raise RuntimeError(
                "YouTube 已触发限流或机器人验证。请换用稳定的网络出口，"
                "再选择自动读取 Google Chrome，或直接粘贴从已登录 "
                "YouTube 会话导出的 cookies.txt 内容"
            )
        raise RuntimeError(f"{label}失败: {_summarize_yt_dlp_error(lines)}")

    def execute(
        args: list[str], label: str, command_prefix: list[str] | None = None,
    ) -> None:
        """Run yt-dlp with streaming stdout/stderr, emitting each line."""
        ok, lines = _run_capture(args, label, command_prefix)
        if ok:
            return
        if allow_missing_subtitles and "--write-auto-subs" in args:
            if emit:
                emit(
                    f"YouTube {_language_label(source_language)}字幕不可用；"
                    "将使用本地 Whisper"
                )
            return
        _raise_failure(label, lines)

    def _download_video_with_fallback(max_height: int | None) -> None:
        """Try the quality strategies in order, reacting to the failure kind.

        三类动作：格式不可用 → 降级到单一流；会话不匹配 → 摘掉 Cookie 改用
        匿名重试；yt-dlp 合并崩溃 → 改用 ffmpeg 本地合并。其余失败类型维持
        原样立即抛出，不额外拖延，也不做无意义的盲重试。
        """
        strategies = _build_quality_strategies(video_quality)
        max_attempts = 3
        attempts = 0
        dropped_cookies = False
        last_label = "视频下载"
        last_lines: list[str] = []

        for index, strategy in enumerate(strategies):
            while attempts < max_attempts:
                attempts += 1
                current_prefix = prefix
                if dropped_cookies and "--cookies" in current_prefix:
                    # 只改内存副本，绝不触碰用户磁盘上的 Cookie 文件。
                    current_prefix = list(current_prefix)
                    position = current_prefix.index("--cookies")
                    del current_prefix[position:position + 2]

                args = ["-f", strategy["format"], "--write-info-json"]
                if strategy["merge_output_format"]:
                    args += [
                        "--merge-output-format",
                        strategy["merge_output_format"],
                    ]
                args += ["-o", str(output / f"{video_id}.%(ext)s"), url]

                if index == 0:
                    label = (
                        f"正在下载视频（{video_quality}p 上限）"
                        if max_height is not None
                        else "正在下载视频（最高可用清晰度）"
                    )
                else:
                    label = f"正在下载视频（{strategy['label']}）"

                ok, lines = _run_capture(args, label, current_prefix)
                last_label, last_lines = label, lines
                if ok:
                    return

                kind = _classify_download_failure("\n".join(lines[-10:]))

                if (
                    kind == FAILURE_MERGE_CRASH
                    and _merge_with_ffmpeg(output, video_id)
                ):
                    if emit:
                        emit("yt-dlp 合并异常，已改用 ffmpeg 本地合并完成")
                    return

                if kind == FAILURE_FORMAT and index + 1 < len(strategies):
                    if emit:
                        emit(
                            "当前清晰度不可用，已自动降级重试"
                            f"（{strategies[index + 1]['label']}）"
                        )
                    break

                if (
                    kind == FAILURE_SESSION_RELOAD
                    and "--cookies" in prefix
                    and not dropped_cookies
                ):
                    dropped_cookies = True
                    if emit:
                        emit("YouTube 会话不匹配，已改用匿名方式重试")
                    continue

                _raise_failure(label, lines)

        _raise_failure(last_label, last_lines)

    # --- Video ---
    if download_video:
        max_height = VIDEO_QUALITY_HEIGHTS[video_quality]
        _download_video_with_fallback(max_height)
        # 降级路径可能产出 webm/mkv 等容器，按后缀定位而不是写死 .mp4。
        video = _pick_video_file(output, video_id)
        if video is None:
            raise RuntimeError("视频下载后文件为空")
        artifacts["video"] = str(video)
        metadata_path = output / f"{video_id}.info.json"
        if metadata_path.exists() and metadata_path.stat().st_size:
            artifacts["metadata_json"] = str(metadata_path)

    # --- Thumbnail ---
    if download_thumbnail:
        execute(
            [
                "--write-thumbnail",
                "--skip-download",
                "-o", str(output / f"{video_id}.%(ext)s"),
                url,
            ],
            "正在下载封面",
        )
        thumbnail = _pick_thumbnail(output, video_id)
        if thumbnail:
            artifacts["thumbnail"] = str(thumbnail)

    # --- Declared-language subtitles ---
    if download_subtitles:
        language_selector = _subtitle_language_selector(source_language)
        execute(
            [
                "--write-subs", "--write-auto-subs", "--sub-lang", language_selector,
                "--sub-format", "vtt",
                "--skip-download",
                "-o", str(output / f"{video_id}.%(ext)s"),
                url,
            ],
            f"正在下载{_language_label(source_language)}字幕",
        )
        subtitle = _pick_source_subtitle(
            output, video_id, source_language,
        ) or output / f"{video_id}.{source_language}.vtt"
        if not subtitle.exists() or subtitle.stat().st_size == 0:
            if allow_missing_subtitles:
                return artifacts
            raise RuntimeError(
                f"没有找到 YouTube {_language_label(source_language)}字幕"
            )
        if subtitle.suffix.casefold() == ".vtt":
            converted_subtitle = subtitle.with_suffix(".srt")
            convert_webvtt_to_srt(subtitle, converted_subtitle)
            subtitle = converted_subtitle
        # 去重：--write-subs + --write-auto-subs 会同时落盘手动版与
        # 自动版（如 xxx.ja.srt 与 xxx.ja-orig.srt，内容常常完全相同）。
        # 只保留选中的那份，其余同语言变体删除，避免任务目录里出现重复文件。
        _dedupe_subtitle_variants(output, video_id, source_language, subtitle)
        cleaned_subtitle = output / f"{video_id}.{source_language}.cleaned.srt"
        prepare_youtube_subtitles(subtitle, cleaned_subtitle, source_language)
        if not cleaned_subtitle.exists() or cleaned_subtitle.stat().st_size == 0:
            if allow_missing_subtitles:
                artifacts[f"youtube_{source_language}_raw"] = str(subtitle)
                return artifacts
            raise RuntimeError(
                f"YouTube {_language_label(source_language)}字幕清洗后没有可用内容"
            )
        artifacts[f"youtube_{source_language}_raw"] = str(subtitle)
        artifacts[f"youtube_{source_language}"] = str(cleaned_subtitle)
        artifacts["youtube_source"] = str(cleaned_subtitle)
        if source_language == "en":
            artifacts["youtube_en_raw"] = str(subtitle)
            artifacts["youtube_en"] = str(cleaned_subtitle)

    return artifacts
