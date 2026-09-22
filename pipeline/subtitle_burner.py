"""Render deliverable subtitles into a separate MP4 with local FFmpeg.

This module deliberately has no Web dependency.  It accepts only trusted file
paths selected by the job manager and never exposes an FFmpeg command or stderr
through its public errors.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms


BLACK_OUTLINE_WHITE_2 = "black-outline-white-2-v1"
_PUBLIC_MESSAGES = {
    "ffmpeg_missing": "未安装 FFmpeg，无法生成带字幕视频",
    "ffprobe_missing": "未安装 FFprobe，无法验证带字幕视频",
    "libass_missing": "当前 FFmpeg 缺少字幕渲染支持（libass）",
    "font_missing": "未找到可用的中文字体，无法生成带字幕视频",
    "burn_input_missing": "生成带字幕视频所需的文件不存在",
    "burn_cancelled": "已取消生成带字幕视频",
    "burn_timeout": "生成带字幕视频超时，可稍后重试",
    "burn_failed": "生成带字幕视频失败，可稍后重试",
}


@dataclass(frozen=True)
class FontChoice:
    name: str
    path: Path


@dataclass(frozen=True)
class BurnStyle:
    width: int
    height: int
    font_name: str
    font_size: int
    outline: float
    margin: int


@dataclass(frozen=True)
class BurnCapability:
    available: bool
    error_code: str | None
    message: str | None
    ffmpeg_path: str | None
    ffprobe_path: str | None
    font: FontChoice | None


@dataclass(frozen=True)
class EncoderChoice:
    name: str
    hardware: bool


@dataclass(frozen=True)
class BurnRequest:
    video_path: Path
    subtitle_path: Path
    output_path: Path


@dataclass(frozen=True)
class BurnResult:
    output_path: Path
    encoder: str
    duration_seconds: float | None


class BurnError(RuntimeError):
    """A stable, public-safe subtitle rendering failure."""

    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(_PUBLIC_MESSAGES.get(error_code, _PUBLIC_MESSAGES["burn_failed"]))


def style_for_resolution(
    width: int,
    height: int,
    *,
    font_name: str,
) -> BurnStyle:
    """Return the confirmed black-outline/white-fill preset for a frame size."""
    width = max(1, int(width))
    height = max(1, int(height))
    return BurnStyle(
        width=width,
        height=height,
        font_name=font_name,
        font_size=max(32, min(84, round(height * 0.06))),
        outline=max(2.0, min(6.0, round(height * 0.004, 1))),
        margin=max(24, min(84, round(height / 18))),
    )


def _ass_timestamp(value: str) -> str:
    centiseconds = timestamp_to_ms(value) // 10
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def _ass_text(value: str) -> str:
    # Braces introduce ASS override tags. Escaping them prevents translated
    # subtitle text from becoming renderer instructions.
    return (
        str(value)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\N")
    )


def write_ass(source: Path, output: Path, style: BurnStyle) -> None:
    """Convert SRT timing/text to a fixed, injection-safe ASS presentation."""
    subtitles = parse_srt(str(source))
    if not subtitles:
        raise BurnError("burn_input_missing")
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {style.width}\n"
        f"PlayResY: {style.height}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,"
        "ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,"
        "MarginR,MarginV,Encoding\n"
        "Style: BlackOutlineWhite2,"
        f"{style.font_name},{style.font_size},&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"{style.outline:.1f},0.0,2,{style.margin},{style.margin},"
        f"{style.margin},1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,"
        "Effect,Text\n"
    )
    events = [
        "Dialogue: 0,"
        f"{_ass_timestamp(item.start)},{_ass_timestamp(item.end)},"
        "BlackOutlineWhite2,,0,0,0,,"
        f"{_ass_text(item.text)}"
        for item in subtitles
    ]
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8", newline="\n")


def _font_directories() -> list[Path]:
    roots: list[Path] = []
    windows_dir = os.getenv("WINDIR", "").strip()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if windows_dir:
        roots.append(Path(windows_dir) / "Fonts")
    if local_app_data:
        roots.append(Path(local_app_data) / "Microsoft" / "Windows" / "Fonts")
    roots.extend((Path("/usr/share/fonts"), Path("/usr/local/share/fonts")))
    return roots


def find_chinese_font() -> FontChoice | None:
    """Find a redistributable-first or installed system Chinese font."""
    candidates = (
        ("Noto Sans CJK SC", "NotoSansCJKsc-Black.otf"),
        ("Noto Sans CJK SC", "NotoSansCJKsc-Bold.otf"),
        ("Source Han Sans SC", "SourceHanSansSC-Heavy.otf"),
        ("Source Han Sans SC", "SourceHanSansSC-Bold.otf"),
        ("Microsoft YaHei", "msyhbd.ttc"),
        ("Microsoft YaHei", "msyh.ttc"),
        ("SimHei", "simhei.ttf"),
    )
    for directory in _font_directories():
        for name, filename in candidates:
            path = directory / filename
            if path.is_file() and not path.is_symlink():
                return FontChoice(name=name, path=path)
    return None


def check_burn_capability(
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> BurnCapability:
    """Check local renderer dependencies without downloading anything."""
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        return BurnCapability(False, "ffmpeg_missing", _PUBLIC_MESSAGES["ffmpeg_missing"], None, None, None)
    ffprobe = ffprobe_path or shutil.which("ffprobe")
    if not ffprobe:
        return BurnCapability(False, "ffprobe_missing", _PUBLIC_MESSAGES["ffprobe_missing"], str(ffmpeg), None, None)
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return BurnCapability(False, "burn_failed", _PUBLIC_MESSAGES["burn_failed"], str(ffmpeg), str(ffprobe), None)
    filters = f"{result.stdout}\n{result.stderr}"
    if result.returncode or not any(
        line.split()[1:2] in (["subtitles"], ["ass"])
        for line in filters.splitlines()
    ):
        return BurnCapability(False, "libass_missing", _PUBLIC_MESSAGES["libass_missing"], str(ffmpeg), str(ffprobe), None)
    font = find_chinese_font()
    if font is None:
        return BurnCapability(False, "font_missing", _PUBLIC_MESSAGES["font_missing"], str(ffmpeg), str(ffprobe), None)
    return BurnCapability(True, None, None, str(ffmpeg), str(ffprobe), font)


def select_encoder(ffmpeg_path: str) -> EncoderChoice:
    """Prefer a listed hardware H.264 encoder and always retain CPU fallback."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        listed = result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        listed = ""
    for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if name in listed:
            return EncoderChoice(name=name, hardware=True)
    return EncoderChoice(name="libx264", hardware=False)


def build_burn_command(
    request: BurnRequest,
    encoder: EncoderChoice,
    work_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    copy_audio: bool = True,
) -> list[str]:
    """Build an argv-only command using controlled names inside ``work_dir``."""
    del request, work_dir  # Paths are staged under fixed names by burn_subtitles.
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        "input.mp4",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "ass=subtitles.ass:fontsdir=fonts",
        "-c:v",
        encoder.name,
    ]
    if encoder.name == "libx264":
        command.extend(("-preset", "medium", "-crf", "20"))
    elif encoder.name == "h264_nvenc":
        command.extend(("-preset", "p4", "-cq", "20"))
    elif encoder.name == "h264_qsv":
        command.extend(("-global_quality", "20"))
    elif encoder.name == "h264_amf":
        command.extend(("-quality", "quality", "-qp_i", "20", "-qp_p", "20"))
    command.extend(("-c:a", "copy" if copy_audio else "aac"))
    if not copy_audio:
        command.extend(("-b:a", "192k"))
    command.extend((
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "output.tmp.mp4",
    ))
    return command


def _probe_video(ffprobe_path: str, path: Path) -> tuple[int, int, float | None]:
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BurnError("burn_input_missing") from error
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    try:
        width = int(values["width"])
        height = int(values["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise BurnError("burn_input_missing") from error
    try:
        duration = float(values.get("duration", ""))
    except ValueError:
        duration = None
    return width, height, duration if duration and duration > 0 else None


def _stage_input(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _run_ffmpeg(
    command: list[str],
    *,
    cwd: Path,
    duration_seconds: float | None,
    on_progress: Callable[[int], None],
    cancel_check: Callable[[], bool],
) -> int:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_progress() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line.rstrip("\r\n"))
        lines.put(None)

    reader = threading.Thread(target=read_progress, name="subtitle-burn-progress", daemon=True)
    reader.start()
    timeout_seconds = max(300.0, min(14_400.0, (duration_seconds or 60.0) * 4.0 + 120.0))
    deadline = time.monotonic() + timeout_seconds
    last_progress = -1
    try:
        while process.poll() is None:
            if cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise BurnError("burn_cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                raise BurnError("burn_timeout")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line and line.startswith("out_time_us=") and duration_seconds:
                try:
                    completed_seconds = int(line.partition("=")[2]) / 1_000_000
                    progress = max(0, min(99, int(completed_seconds * 100 / duration_seconds)))
                except (TypeError, ValueError):
                    continue
                if progress > last_progress:
                    last_progress = progress
                    on_progress(progress)
        return int(process.returncode or 0)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=1)


def burn_subtitles(
    request: BurnRequest,
    *,
    on_progress: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> BurnResult:
    """Render a separate MP4 and atomically publish it after validation."""
    progress_callback = on_progress or (lambda _value: None)
    is_cancelled = cancel_check or (lambda: False)
    video = Path(request.video_path)
    subtitles = Path(request.subtitle_path)
    output = Path(request.output_path)
    if any(not path.is_file() or path.is_symlink() for path in (video, subtitles)):
        raise BurnError("burn_input_missing")
    capability = check_burn_capability()
    if not capability.available:
        raise BurnError(capability.error_code or "burn_failed")
    assert capability.ffmpeg_path and capability.ffprobe_path and capability.font
    width, height, duration = _probe_video(capability.ffprobe_path, video)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f".subtitle-burn-{uuid.uuid4().hex}"
    work_dir.mkdir()
    staged_video = work_dir / "input.mp4"
    staged_ass = work_dir / "subtitles.ass"
    staged_output = work_dir / "output.tmp.mp4"
    fonts_dir = work_dir / "fonts"
    fonts_dir.mkdir()
    try:
        _stage_input(video, staged_video)
        shutil.copy2(capability.font.path, fonts_dir / capability.font.path.name)
        write_ass(
            subtitles,
            staged_ass,
            style_for_resolution(width, height, font_name=capability.font.name),
        )
        selected = select_encoder(capability.ffmpeg_path)
        attempts = [
            (selected, True),
            *(
                [(EncoderChoice("libx264", False), True)]
                if selected.hardware else []
            ),
            (EncoderChoice("libx264", False), False),
        ]
        used_encoder: EncoderChoice | None = None
        for encoder, copy_audio in attempts:
            staged_output.unlink(missing_ok=True)
            command = build_burn_command(
                request,
                encoder,
                work_dir,
                ffmpeg_path=capability.ffmpeg_path,
                copy_audio=copy_audio,
            )
            code = _run_ffmpeg(
                command,
                cwd=work_dir,
                duration_seconds=duration,
                on_progress=progress_callback,
                cancel_check=is_cancelled,
            )
            if code == 0 and staged_output.is_file() and staged_output.stat().st_size > 0:
                try:
                    _probe_video(capability.ffprobe_path, staged_output)
                except BurnError:
                    continue
                used_encoder = encoder
                break
        if used_encoder is None:
            raise BurnError("burn_failed")
        os.replace(staged_output, output)
        progress_callback(100)
        return BurnResult(output_path=output, encoder=used_encoder.name, duration_seconds=duration)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
