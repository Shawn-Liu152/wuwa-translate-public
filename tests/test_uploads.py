import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from web import app as app_module


def test_upload_at_limit_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 8)
    upload = UploadFile(filename="ok.srt", file=BytesIO(b"12345678"))

    path = asyncio.run(app_module.persist_upload(upload))
    try:
        assert path.read_bytes() == b"12345678"
    finally:
        path.unlink(missing_ok=True)


def test_upload_over_limit_returns_413_and_cleans_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 8)
    original_mkstemp = app_module.tempfile.mkstemp

    def fixed_mkstemp(*args, **kwargs):
        return original_mkstemp(dir=tmp_path, suffix=".srt")

    monkeypatch.setattr(app_module.tempfile, "mkstemp", fixed_mkstemp)
    upload = UploadFile(filename="large.srt", file=BytesIO(b"123456789"))

    with pytest.raises(HTTPException) as raised:
        asyncio.run(app_module.persist_upload(upload))
    assert raised.value.status_code == 413
    assert not list(tmp_path.iterdir())


def test_upload_read_error_cleans_temp(tmp_path, monkeypatch):
    original_mkstemp = app_module.tempfile.mkstemp

    def fixed_mkstemp(*args, **kwargs):
        return original_mkstemp(dir=tmp_path, suffix=".srt")

    class BrokenUpload:
        filename = "broken.srt"

        async def read(self, size):
            raise OSError("read failed")

    monkeypatch.setattr(app_module.tempfile, "mkstemp", fixed_mkstemp)
    with pytest.raises(OSError):
        asyncio.run(app_module.persist_upload(BrokenUpload()))
    assert not list(tmp_path.iterdir())


def test_empty_upload_is_rejected_and_cleaned(tmp_path, monkeypatch):
    original_mkstemp = app_module.tempfile.mkstemp

    def fixed_mkstemp(*args, **kwargs):
        return original_mkstemp(dir=tmp_path, suffix=".srt")

    monkeypatch.setattr(app_module.tempfile, "mkstemp", fixed_mkstemp)
    upload = UploadFile(filename="empty.srt", file=BytesIO(b""))

    with pytest.raises(HTTPException) as raised:
        asyncio.run(app_module.persist_upload(upload))
    assert raised.value.status_code == 422
    assert not list(tmp_path.iterdir())


def test_multifile_failure_cleans_previous_upload(tmp_path, monkeypatch):
    first = tmp_path / "first.srt"
    calls = 0

    async def persist(upload):
        nonlocal calls
        calls += 1
        if calls == 1:
            first.write_bytes(b"first")
            return first
        raise HTTPException(413, "too large")

    monkeypatch.setattr(app_module, "persist_upload", persist)
    capcut = SimpleUpload("capcut.srt")
    whisper = SimpleUpload("whisper.srt")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(app_module.create_job(
            capcut_en=capcut, whisper_en=whisper, video=None,
        ))
    assert raised.value.status_code == 413
    assert not first.exists()


class SimpleUpload:
    def __init__(self, filename):
        self.filename = filename


def test_prompt_template_requires_pipeline_variables(tmp_path, monkeypatch):
    target = tmp_path / "data" / "prompts" / "en-zh-CN.txt"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(app_module.update_prompt_template({"content": "{subtitles}"}))

    assert raised.value.status_code == 422
    assert target.read_text(encoding="utf-8") == "old"


def test_valid_prompt_template_is_saved_atomically(tmp_path, monkeypatch):
    target = tmp_path / "data" / "prompts" / "en-zh-CN.txt"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    content = (
        "{glossary}\n{established_terms_section}\n{tricky_terms}\n"
        "{subtitles}\n{format_example}\n{{literal}}"
    )

    result = asyncio.run(app_module.update_prompt_template({"content": content}))

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == content
