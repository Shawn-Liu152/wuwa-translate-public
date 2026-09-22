import sys
import types
import os

from pipeline.tools import whisper_transcribe


def test_nvidia_dll_directories_are_added_to_process_path(tmp_path, monkeypatch):
    site_packages = tmp_path / "site-packages"
    expected = []
    for package in ("cudnn", "cublas", "cuda_runtime"):
        path = site_packages / "nvidia" / package / "bin"
        path.mkdir(parents=True)
        expected.append(str(path))
    monkeypatch.setenv("PATH", "existing-path")
    monkeypatch.setattr(whisper_transcribe, "_DLL_DIRECTORY_HANDLES", [])

    added = whisper_transcribe._configure_nvidia_dll_paths([site_packages])

    assert added == expected
    process_paths = os.environ["PATH"].split(os.pathsep)
    assert process_paths[:3] == expected


def test_nvidia_dll_discovery_skips_protected_optional_paths(monkeypatch):
    original = whisper_transcribe.Path.is_dir

    def protected(path):
        if str(path).endswith("blocked"):
            raise PermissionError("protected")
        return original(path)

    monkeypatch.setattr(whisper_transcribe.Path, "is_dir", protected)

    assert whisper_transcribe._configure_nvidia_dll_paths(["blocked"]) == []


def test_transcribe_retries_without_vad_when_onnxruntime_is_unavailable(
        monkeypatch):
    calls = []

    class Info:
        duration = 2.0

    class Segment:
        text = "Hello"
        start = 0.0
        end = 1.0
        words = []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            calls.append(kwargs["vad_filter"])
            if kwargs["vad_filter"]:
                def broken_segments():
                    raise RuntimeError(
                        "Applying the VAD filter requires the onnxruntime package"
                    )
                    yield None
                return broken_segments(), Info()
            return iter([Segment()]), Info()

    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=Model),
    )

    subtitles = whisper_transcribe.transcribe(
        "example.wav", model_size="test", use_vad=True,
    )

    assert calls == [True, False]
    assert [item.text for item in subtitles] == ["Hello"]


def test_transcribe_retries_when_vad_fails_during_the_api_call(monkeypatch):
    calls = []

    class Info:
        duration = 2.0

    class Segment:
        text = "Hello"
        start = 0.0
        end = 1.0
        words = []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            calls.append(kwargs["vad_filter"])
            if kwargs["vad_filter"]:
                raise RuntimeError(
                    "Applying the VAD filter requires the onnxruntime package"
                )
            return iter([Segment()]), Info()

    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=Model),
    )

    subtitles = whisper_transcribe.transcribe(
        "example.wav", model_size="test", use_vad=True,
    )

    assert calls == [True, False]
    assert [item.text for item in subtitles] == ["Hello"]


def test_transcribe_retries_low_coverage_with_relaxed_vad(monkeypatch):
    calls = []

    class Info:
        duration = 100.0

    class Segment:
        words = []

        def __init__(self, text, end):
            self.text = text
            self.start = 0.0
            self.end = end

    class Model:
        instances = 0

        def __init__(self, *args, **kwargs):
            type(self).instances += 1

        def transcribe(self, *args, **kwargs):
            calls.append(kwargs["vad_parameters"])
            if len(calls) == 1:
                return iter([Segment("weak fragment", 0.5)]), Info()
            return iter([Segment("clear speech", 20.0)]), Info()

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model)
    )

    subtitles = whisper_transcribe.transcribe(
        "example.wav", model_size="test", use_vad=True,
        vad_min_coverage=0.015,
    )

    assert [round(call["threshold"], 2) for call in calls] == [0.35, 0.25]
    assert Model.instances == 1
    assert [item.text for item in subtitles] == ["clear speech"]


def test_japanese_transcription_uses_japanese_prompt_and_keeps_words_together(
        monkeypatch):
    observed = {}

    class Info:
        duration = 2.0

    class Word:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class Segment:
        text = "\u79c1\u306f\u5927\u4e08\u592b\u3067\u3059"
        start = 0.0
        end = 1.0
        words = [
            Word("\u79c1", 0.0, 0.2),
            Word("\u306f", 0.2, 0.4),
            Word("\u5927\u4e08\u592b\u3067\u3059\u3002", 0.4, 1.0),
        ]

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            observed.update(kwargs)
            return iter([Segment()]), Info()

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model)
    )

    subtitles = whisper_transcribe.transcribe(
        "example.wav", model_size="test", language="ja", use_vad=False,
    )

    assert "\u30ab\u30eb\u30c6\u30b8\u30a2" in observed["initial_prompt"]
    assert subtitles[0].text == "\u79c1\u306f\u5927\u4e08\u592b\u3067\u3059\u3002"


def test_korean_transcription_uses_korean_prompt_and_keeps_words_together(
        monkeypatch):
    observed = {}

    class Info:
        duration = 2.0

    class Word:
        def __init__(self, word, start, end):
            self.word, self.start, self.end = word, start, end

    class Segment:
        text = "카르테시아 봤어요?"
        start, end = 0.0, 1.0
        words = [Word("카르테시아", 0.0, 0.5), Word("봤어요?", 0.5, 1.0)]

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            observed.update(kwargs)
            return iter([Segment()]), Info()

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model)
    )

    subtitles = whisper_transcribe.transcribe(
        "example.wav", model_size="test", language="ko", use_vad=True,
    )

    assert "명조" in observed["initial_prompt"]
    assert observed["vad_filter"] is True
    assert subtitles[0].text == "카르테시아 봤어요?"


def test_temp_audio_cleanup_failure_is_not_fatal(tmp_path, monkeypatch):
    """os.remove PermissionError during temp cleanup must not crash main().

    Regression: Windows file handles (ffmpeg/antivirus) can briefly lock the
    extracted wav; the SRT is already written and must not be discarded just
    because cleanup failed.
    """
    import argparse
    srt = tmp_path / "out.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")

    def fake_transcribe(*args, **kwargs):
        return []

    def fake_remove(path):
        raise PermissionError(32, "file locked")

    monkeypatch.setattr(whisper_transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(os, "remove", fake_remove)
    # main() calls log.warning — make sure it does not raise
    monkeypatch.setattr(
        whisper_transcribe, "save_srt",
        lambda *a, **k: None,
    )

    argv = [
        "whisper_transcribe.py",
        str(tmp_path / "input.mp4"),
        "-o", str(srt),
    ]
    monkeypatch.setattr(argparse, "_sys", types.SimpleNamespace(argv=argv))
    # extract_audio returns a temp wav different from input
    monkeypatch.setattr(
        whisper_transcribe, "extract_audio",
        lambda inp: str(tmp_path / "temp.wav"),
    )

    whisper_transcribe.main()  # must not raise
