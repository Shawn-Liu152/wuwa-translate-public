"""
Whisper 语音识别脚本 — 接入鸣潮字幕翻译管道

功能：
1. 用 faster-whisper (GPU) 识别视频/音频生成 SRT 字幕
2. 支持注入鸣潮术语 prompt 提高专有名词识别率
3. VAD 过滤静音段加速识别
4. 识别完成后可直接接入翻译管道

使用方式：
    python scripts/whisper_transcribe.py <video_or_audio> [-o output.srt] [--model medium] [--language en]

示例：
    python scripts/whisper_transcribe.py download/k2qMx9P8vBk.mp4 -o download/k2qMx9P8vBk.en.srt
    python scripts/whisper_transcribe.py download/k2qMx9P8vBk.mp4 --model large-v3 --language en
"""
import argparse
import json
import os
import sys
import time
import logging
import subprocess
from pathlib import Path

# 添加项目根目录
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

# --- CUDA DLL 路径设置（faster-whisper GPU 必需）---
# CTranslate2 在 Windows 上通过进程 PATH 延迟加载 cuDNN/cuBLAS。仅调用
# os.add_dll_directory 对它并不可靠，因此同时更新 PATH，并保留目录句柄。
_DLL_DIRECTORY_HANDLES = []


def _safe_is_dir(path: Path) -> bool:
    """Treat protected optional runtime folders as unavailable."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _configure_nvidia_dll_paths(search_roots=None) -> list[str]:
    if search_roots is None:
        roots = [Path(value) for value in sys.path if value]
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            python_root = Path(local_app_data) / "Programs" / "Python"
            if _safe_is_dir(python_root):
                try:
                    roots.extend(python_root.glob("Python*/Lib/site-packages"))
                except OSError:
                    pass
    else:
        roots = [Path(value) for value in search_roots]

    discovered = []
    seen = set()
    for root in roots:
        nvidia_root = root if root.name.casefold() == "nvidia" else root / "nvidia"
        for package in ("cudnn", "cublas", "cuda_runtime"):
            dll_dir = nvidia_root / package / "bin"
            if not _safe_is_dir(dll_dir):
                continue
            key = os.path.normcase(str(dll_dir.resolve()))
            if key in seen:
                continue
            seen.add(key)
            discovered.append(str(dll_dir))

    if discovered:
        current = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(
            discovered + ([current] if current else [])
        )
        add_directory = getattr(os, "add_dll_directory", None)
        if add_directory is not None:
            for dll_dir in discovered:
                try:
                    _DLL_DIRECTORY_HANDLES.append(add_directory(dll_dir))
                except OSError:
                    pass
    return discovered


_NVIDIA_DLL_PATHS = _configure_nvidia_dll_paths()

from pipeline.parser.srt_parser import Subtitle, save_srt, timestamp_to_ms

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

# 鸣潮术语 prompt（提高 Whisper 识别专有名词的准确率）
WUWA_PROMPT = (
    "Wuthering Waves, Rover, Changli, Scar, Encore, Camelia, Mornye, "
    "Jinhsi, Yuanwu, Sanhua, Lingyang, Jianxin, Calcharo, Verina, "
    "Baizhi, Danjin, Mortefi, Aalto, Chixia, Taoqi, Yangyang, "
    "Lament, Tacet, Waveworn, Sonata, Echo, Resonator, Tower of Adversity, "
    "C6, S6, broken, aura farms, sentinel, Irelia, League of Legends, "
    "sentry core, sword formations, screenshots, main character, "
    "Cantarella, Phrolova, Geshu Lin, Cartethyia, Zani, Brant, Roccia, "
    "Fleurdelys, Imperator, Sentinel Jue, Threnodian, Tacet Discord, "
    "Resonance Liberation, Forte Circuit, Intro Skill, Outro Skill, "
    "What the fuck, Holy shit, damn, let's go, bro"
)

# 日语初始提示词只提供已确认的游戏术语与角色名，避免把英语提示词当作
# 日语识别上下文。它不是翻译词典，也不改变输出语言。
WUWA_JA_PROMPT = (
    "鳴潮、漂泊者、長離、カルテジア、フィービー、フローヴァ、"
    "ザニ、ブラント、ロココ、今汐、忌炎、散華、秧秧、吟霖、"
    "共鳴者、音骸、共鳴解放、共鳴スキル、変奏スキル、終奏スキル、"
    "逆境深塔、ソナタ効果"
)

WUWA_KO_PROMPT = (
    "명조, 방랑자, 카르테시아, 피비, 프롤로바, 자니, 브란트, 로코코, "
    "금희, 기염, 산화, 양양, 음림, 공명자, 에코, 공명 해방, 공명 스킬, "
    "변주 스킬, 반주 스킬, 역경의 탑"
)


def initial_prompt_for_language(language: str) -> str | None:
    language = str(language or "").strip().lower()
    if language == "ja":
        return WUWA_JA_PROMPT
    if language == "ko":
        return WUWA_KO_PROMPT
    if language == "en":
        return WUWA_PROMPT
    return None


def format_timestamp(seconds: float) -> str:
    """将秒数转换为 SRT 时间戳格式 HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def extract_audio(video_path: str, output_path: str = None) -> str:
    """
    用 ffmpeg 从视频提取 16kHz 单声道 WAV 音频。
    Whisper 在音频上比直接喂视频更快。
    """
    if output_path is None:
        base = os.path.splitext(video_path)[0]
        output_path = f"{base}_audio.wav"

    log.info(f"提取音频: {video_path} → {output_path}")

    result = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn",              # 去掉视频
        "-ac", "1",         # 单声道
        "-ar", "16000",     # 16kHz（Whisper 要求）
        "-c:a", "pcm_s16le",
        output_path
    ], capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"ffmpeg 提取音频失败: {result.stderr[-500:]}")
        return video_path  # 失败则直接用原文件

    log.info("音频提取完成 (16kHz, mono, WAV)")
    return output_path


def transcribe(audio_path: str,
               model_size: str = "medium",
               language: str = "en",
               output_path: str = None,
               use_vad: bool = True,
               beam_size: int = 3,
               compute_type: str = "float16",
               vad_min_coverage: float = 0.015,
               _vad_retry: bool = False,
               _vad_parameters: dict = None,
               _model=None) -> list:
    """
    用 faster-whisper 识别音频，生成字幕列表。

    Args:
        audio_path: 音频/视频文件路径
        model_size: 模型大小 (tiny/base/small/medium/large-v3)
        language: 语言代码 (en/zh/ja...)
        output_path: 输出 SRT 文件路径
        use_vad: 是否启用 VAD 静音过滤
        beam_size: 搜索宽度（越小越快）
        compute_type: 计算精度 (int8_float16/float16/float32)

    Returns:
        List[Subtitle]
    """
    from faster_whisper import WhisperModel

    log.info("=" * 50)
    log.info("Whisper 语音识别")
    log.info(f"  音频: {audio_path}")
    log.info(f"  模型: {model_size}")
    log.info(f"  语言: {language}")
    log.info(f"  VAD: {'开启' if use_vad else '关闭'}")
    log.info(f"  beam_size: {beam_size}")
    log.info(f"  compute_type: {compute_type}")
    log.info("=" * 50)

    # 加载模型
    log.info("[1/3] 加载模型...")
    t0 = time.time()
    model = _model or WhisperModel(model_size, device="cuda", compute_type=compute_type)
    log.info(f"  模型加载耗时: {time.time()-t0:.1f}s")

    # 识别
    log.info("[2/3] 开始识别...")
    t0 = time.time()

    try:
        segments_iter, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            vad_filter=use_vad,
            word_timestamps=True,
            condition_on_previous_text=False,
            max_new_tokens=128,
            vad_parameters=(_vad_parameters or dict(
                min_silence_duration_ms=1000,  # 只切1秒以上的长静音
                speech_pad_ms=100,
                threshold=0.35,  # 降低灵敏度，避免把短停顿当静音
            )),
            initial_prompt=initial_prompt_for_language(language),
        )
    except RuntimeError as error:
        message = str(error).casefold()
        if use_vad and ("onnxruntime" in message or "vad filter" in message):
            log.warning(
                "VAD 组件不可用，已自动关闭 VAD 并重新开始识别：%s", error
            )
            return transcribe(
                audio_path=audio_path,
                model_size=model_size,
                language=language,
                output_path=output_path,
                use_vad=False,
                beam_size=beam_size,
                compute_type=compute_type,
                vad_min_coverage=vad_min_coverage,
                _vad_retry=True,
                _model=model,
            )
        raise

    # 转换为 Subtitle 列表：使用词级时间戳重新切分，避免 Whisper 长段覆盖静音/游戏台词
    subtitles = []
    next_progress = 10
    try:
        for seg in segments_iter:
            duration = max(float(getattr(info, "duration", 0) or 0), 0.001)
            progress = min(100, int(float(getattr(seg, "end", 0) or 0) / duration * 100))
            if progress >= next_progress:
                log.info("[2/3] 识别进度: %d%%", progress)
                next_progress = min(100, (progress // 10 + 1) * 10)
            words = getattr(seg, "words", None) or []
            timed_words = [w for w in words if getattr(w, "word", "").strip()
                           and getattr(w, "start", None) is not None
                           and getattr(w, "end", None) is not None]

            if not timed_words:
                text = seg.text.strip()
                if text:
                    subtitles.append(Subtitle(
                        id=len(subtitles) + 1,
                        start=format_timestamp(seg.start),
                        end=format_timestamp(seg.end),
                        text=text,
                    ))
                continue

            chunk = []
            chunk_start = timed_words[0].start
            previous_end = timed_words[0].end

            def flush_chunk():
                if not chunk:
                    return
                joiner = "" if language == "ja" else " "
                text = joiner.join(w.word.strip() for w in chunk).strip()
                if text:
                    subtitles.append(Subtitle(
                        id=len(subtitles) + 1,
                        start=format_timestamp(chunk[0].start),
                        end=format_timestamp(chunk[-1].end),
                        text=text,
                    ))

            for word in timed_words:
                if not chunk:
                    chunk_start = word.start
                pause = word.start - previous_end
                word_duration = word.end - chunk_start
                word_text = word.word.strip()
                should_split_before = (
                    chunk and (pause >= 0.75 or word_duration >= 6.0
                               )
                )
                if should_split_before:
                    flush_chunk()
                    chunk = []
                    chunk_start = word.start
                chunk.append(word)
                previous_end = word.end
                if word_text.endswith((
                    ".", "!", "?", "。", "！", "？", "…",
                )):
                    flush_chunk()
                    chunk = []
            flush_chunk()
    except RuntimeError as error:
        message = str(error).casefold()
        if use_vad and ("onnxruntime" in message or "vad filter" in message):
            log.warning(
                "VAD 组件不可用，已自动关闭 VAD 并重新开始识别：%s", error
            )
            return transcribe(
                audio_path=audio_path,
                model_size=model_size,
                language=language,
                output_path=output_path,
                use_vad=False,
                beam_size=beam_size,
                compute_type=compute_type,
                vad_min_coverage=vad_min_coverage,
                _vad_retry=True,
                _model=model,
            )
        raise

    audio_duration = max(float(getattr(info, "duration", 0) or 0), 0.001)
    speech_seconds = sum(
        max(0, timestamp_to_ms(item.end) - timestamp_to_ms(item.start))
        for item in subtitles
    ) / 1000.0
    coverage = speech_seconds / audio_duration
    if use_vad and not _vad_retry and coverage < max(0.0, vad_min_coverage):
        relaxed_parameters = {
            "min_silence_duration_ms": 600,
            "speech_pad_ms": 160,
            "threshold": 0.25,
        }
        log.warning(
            "VAD 语音覆盖率 %.4f 低于 %.4f，使用宽松参数复检",
            coverage, vad_min_coverage,
        )
        relaxed = transcribe(
            audio_path=audio_path, model_size=model_size, language=language,
            output_path=None, use_vad=True, beam_size=beam_size,
            compute_type=compute_type, vad_min_coverage=vad_min_coverage,
            _vad_retry=True, _vad_parameters=relaxed_parameters,
            _model=model,
        )
        relaxed_seconds = sum(
            max(0, timestamp_to_ms(item.end) - timestamp_to_ms(item.start))
            for item in relaxed
        ) / 1000.0
        if relaxed_seconds > speech_seconds:
            subtitles = relaxed
            coverage = relaxed_seconds / audio_duration
            log.info("宽松 VAD 复检覆盖率提升至 %.4f，采用复检结果", coverage)
        else:
            log.info("宽松 VAD 未改善覆盖率，保留首次识别结果")

    elapsed = time.time() - t0
    speed = audio_duration / elapsed if elapsed > 0 else 0

    log.info(f"  识别耗时: {elapsed:.1f}s")
    log.info(f"  音频时长: {audio_duration:.1f}s ({audio_duration/3600:.1f}h)")
    log.info(f"  识别速度: {speed:.1f}x 实时")
    log.info(f"  字幕条数: {len(subtitles)}")

    # 写入 SRT
    if output_path:
        log.info(f"[3/3] 写入 SRT: {output_path}")
        save_srt(output_path, subtitles)
        log.info(f"  完成!")

    return subtitles


def transcribe_segment(audio_path: str,
                       start_sec: float,
                       end_sec: float,
                       model_size: str = "medium",
                       language: str = "en",
                       padding_sec: float = 1.5,
                       beam_size: int = 5,
                       compute_type: str = "float16") -> list:
    """Re-transcribe one disputed source-audio window and map timestamps back.

    The caller uses this only as evidence for a risk item. It intentionally does
    not write or replace the main SRT/translation.
    """
    if end_sec <= start_sec:
        raise ValueError("局部识别结束时间必须晚于开始时间")
    clip_start = max(0.0, start_sec - padding_sec)
    clip_duration = end_sec - start_sec + padding_sec * 2
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cuda", compute_type=compute_type)
    segments, _ = model.transcribe(
        audio_path, language=language, beam_size=beam_size,
        vad_filter=False, word_timestamps=True,
        condition_on_previous_text=False, clip_timestamps=f"{clip_start},{clip_start + clip_duration}",
        initial_prompt=initial_prompt_for_language(language),
    )
    subtitles = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            subtitles.append(Subtitle(
                id=len(subtitles) + 1,
                start=format_timestamp(segment.start + clip_start),
                end=format_timestamp(segment.end + clip_start),
                text=text,
            ))
    return subtitles


def _transcribe_window_with_model(model, audio_path: str, start_sec: float,
                                  end_sec: float, language: str,
                                  beam_size: int) -> list:
    """Transcribe one absolute source window using an already loaded model."""
    if end_sec <= start_sec:
        raise ValueError("Window end must be later than start")
    segments, _ = model.transcribe(
        audio_path,
        language=language,
        beam_size=beam_size,
        vad_filter=False,
        word_timestamps=True,
        condition_on_previous_text=False,
        clip_timestamps=f"{start_sec},{end_sec}",
        initial_prompt=initial_prompt_for_language(language),
    )
    subtitles = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        # Current faster-whisper versions report absolute clip timestamps.
        # Older builds may return offsets, so only shift when clearly relative.
        segment_start = float(segment.start)
        segment_end = float(segment.end)
        if start_sec > 1 and segment_start < start_sec - 0.5:
            segment_start += start_sec
            segment_end += start_sec
        subtitles.append(Subtitle(
            id=len(subtitles) + 1,
            start=format_timestamp(segment_start),
            end=format_timestamp(segment_end),
            text=text,
        ))
    return subtitles


def transcribe_windows(audio_path: str, windows: list[dict],
                       model_size: str = "medium", language: str = "en",
                       beam_size: int = 5,
                       compute_type: str = "float16") -> None:
    """Load Whisper once and write every requested evidence window."""
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model once for %d risk windows", len(windows))
    model = WhisperModel(model_size, device="cuda", compute_type=compute_type)
    for position, window in enumerate(windows, start=1):
        start_sec = float(window["start"])
        end_sec = float(window["end"])
        output = Path(str(window["output"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        log.info(
            "Risk window %d/%d: %.2fs-%.2fs",
            position, len(windows), start_sec, end_sec,
        )
        subtitles = _transcribe_window_with_model(
            model, audio_path, start_sec, end_sec, language, beam_size,
        )
        save_srt(str(output), subtitles)


def main():
    parser = argparse.ArgumentParser(
        description="Whisper 语音识别 — 生成 SRT 字幕（接入鸣潮翻译管道）"
    )
    parser.add_argument("input", help="输入视频/音频文件路径")
    parser.add_argument("-o", "--output", default=None,
                        help="输出 SRT 文件路径 (默认: <input>.srt)")
    parser.add_argument("--model", default="distil-large-v3",
                        choices=["tiny", "base", "small", "medium", "large-v3", "distil-large-v3", "turbo"],
                        help="模型大小 (默认: distil-large-v3)")
    parser.add_argument("--language", default="en",
                        help="语言代码 (默认: en)")
    parser.add_argument("--no-vad", action="store_true",
                        help="关闭 VAD 静音过滤")
    parser.add_argument("--vad-min-coverage", type=float, default=0.015,
                        help="VAD 结果最低语音覆盖率，低于此值时宽松复检")
    parser.add_argument("--beam-size", type=int, default=3,
                        help="搜索宽度 (默认: 3)")
    parser.add_argument("--compute-type", default="float16",
                        choices=["int8_float16", "float16", "float32"],
                        help="计算精度 (默认: float16)")
    parser.add_argument("--skip-extract", action="store_true",
                        help="跳过音频提取（输入已经是音频文件）")
    parser.add_argument("--start", type=float, default=None,
                        help="仅重识别此源视频秒数开始的局部窗口")
    parser.add_argument("--end", type=float, default=None,
                        help="仅重识别此源视频秒数结束的局部窗口")
    parser.add_argument("--padding", type=float, default=1.5,
                        help="局部识别前后保留秒数（默认 1.5）")

    parser.add_argument(
        "--windows-json",
        default=None,
        help="JSON list of {start,end,output} windows; loads the model only once",
    )

    args = parser.parse_args()

    if args.output is None and not args.windows_json:
        base = os.path.splitext(args.input)[0]
        args.output = f"{base}.srt"

    # 提取音频（如果不是 wav 且未跳过）
    audio_path = args.input
    if not args.skip_extract and not args.input.lower().endswith('.wav'):
        audio_path = extract_audio(args.input)

    # 识别：局部模式禁用 VAD，保留争议段的全部音频作为英文证据。
    if args.windows_json and (args.start is not None or args.end is not None):
        parser.error("--windows-json cannot be combined with --start/--end")
    if (args.start is None) != (args.end is None):
        parser.error("--start 和 --end 必须同时提供")
    if args.windows_json:
        windows = json.loads(Path(args.windows_json).read_text(encoding="utf-8"))
        if not isinstance(windows, list):
            parser.error("--windows-json must contain a JSON list")
        transcribe_windows(
            audio_path=audio_path,
            windows=windows,
            model_size=args.model,
            language=args.language,
            beam_size=args.beam_size,
            compute_type=args.compute_type,
        )
    elif args.start is not None:
        subtitles = transcribe_segment(
            audio_path=audio_path,
            start_sec=args.start,
            end_sec=args.end,
            model_size=args.model,
            language=args.language,
            padding_sec=args.padding,
            beam_size=args.beam_size,
            compute_type=args.compute_type,
        )
        save_srt(args.output, subtitles)
        log.info(f"局部英文证据已写入: {args.output} ({len(subtitles)} 条)")
    else:
        transcribe(
            audio_path=audio_path,
            model_size=args.model,
            language=args.language,
            output_path=args.output,
            use_vad=not args.no_vad,
            beam_size=args.beam_size,
            compute_type=args.compute_type,
            vad_min_coverage=args.vad_min_coverage,
        )

    # 清理临时音频
    if audio_path != args.input and os.path.exists(audio_path):
        try:
            os.remove(audio_path)
            log.info("已清理临时音频: %s", os.path.basename(audio_path))
        except OSError:
            # Windows: ffmpeg/杀软可能短暂占用句柄；清理失败不应推翻已生成的 SRT
            log.warning("临时音频清理失败（不影响转写结果）")


if __name__ == "__main__":
    main()
