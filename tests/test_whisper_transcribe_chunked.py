import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import whisper_transcribe_chunked as chunked


def _capture_atomic_chunk_path(tmp_path, monkeypatch):
    real_mkstemp = chunked.tempfile.mkstemp
    created = []

    def local_mkstemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        descriptor, raw_path = real_mkstemp(*args, **kwargs)
        created.append((descriptor, Path(raw_path)))
        return descriptor, raw_path

    monkeypatch.setattr(chunked.tempfile, "mkstemp", local_mkstemp)
    monkeypatch.setattr(
        chunked.tempfile,
        "mktemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("tempfile.mktemp must not be used")
        ),
    )
    return created


def test_chunk_output_is_atomically_reserved_closed_and_removed(
        tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "output.srt"
    audio.write_bytes(b"audio")
    created = _capture_atomic_chunk_path(tmp_path, monkeypatch)

    def fake_run(command, **kwargs):
        chunk_path = Path(command[5])
        assert chunk_path.is_file()
        descriptor, reserved_path = created[-1]
        assert reserved_path == chunk_path
        try:
            os.fstat(descriptor)
        except OSError:
            pass
        else:
            raise AssertionError("chunk descriptor must close before child launch")
        chunk_path.write_text(
            '[{"start":0.0,"end":1.0,"text":"hello"}]',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(chunked.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv",
        ["whisper_transcribe_chunked.py", str(audio), str(output), "--duration", "1"],
    )

    assert chunked.main() == 0
    assert output.is_file()
    assert created and all(not path.exists() for _, path in created)


def test_chunk_output_is_removed_after_timeout(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "output.srt"
    audio.write_bytes(b"audio")
    created = _capture_atomic_chunk_path(tmp_path, monkeypatch)

    def timeout_after_partial_output(command, **kwargs):
        Path(command[5]).write_text("PRIVATE_PARTIAL_OUTPUT", encoding="utf-8")
        raise subprocess.TimeoutExpired(command, 1800)

    monkeypatch.setattr(chunked.subprocess, "run", timeout_after_partial_output)
    monkeypatch.setattr(
        sys, "argv",
        ["whisper_transcribe_chunked.py", str(audio), str(output), "--duration", "1"],
    )

    assert chunked.main() == 1
    assert created and all(not path.exists() for _, path in created)
    assert not output.exists()


def test_chunk_output_is_removed_after_unexpected_child_error(
        tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "output.srt"
    audio.write_bytes(b"audio")
    created = _capture_atomic_chunk_path(tmp_path, monkeypatch)

    def fail_after_partial_output(command, **kwargs):
        Path(command[5]).write_text("PRIVATE_PARTIAL_OUTPUT", encoding="utf-8")
        raise OSError("PRIVATE_CHILD_FAILURE")

    monkeypatch.setattr(chunked.subprocess, "run", fail_after_partial_output)
    monkeypatch.setattr(
        sys, "argv",
        ["whisper_transcribe_chunked.py", str(audio), str(output), "--duration", "1"],
    )

    with pytest.raises(OSError, match="PRIVATE_CHILD_FAILURE"):
        chunked.main()
    assert created and all(not path.exists() for _, path in created)
    assert not output.exists()
