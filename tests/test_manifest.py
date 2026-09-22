import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.pipeline_manifest import (
    create_manifest, load_manifest, partial_batch_results, save_manifest, set_batch,
    successful_batch_results, validate_resume,
)
from pipeline.parser.srt_parser import Subtitle


def test_manifest_roundtrip_and_success_result():
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as source:
        source.write(b"subtitle input")
        input_path = source.name
    manifest_path = input_path + ".manifest.json"
    options = {"model": "model-a", "batch_size": 20, "game": "wuwa", "term_anchor": True, "refine": False}
    try:
        manifest = create_manifest(input_path, options, [{"key": "1", "id": 1}])
        set_batch(manifest, "0", "running", ids=[1])
        set_batch(manifest, "0", "success", ids=[1], results=[{"id": 1, "original": "hello", "translated": "你好", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "cached": False}])
        save_manifest(manifest_path, manifest)
        loaded = load_manifest(manifest_path)
        validate_resume(loaded, input_path, options)
        assert successful_batch_results(loaded, "0")[0]["translated"] == "你好"
    finally:
        for path in (input_path, manifest_path):
            if os.path.exists(path):
                os.unlink(path)


def test_resume_rejects_changed_input():
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as source:
        source.write(b"old")
        input_path = source.name
    options = {"model": "model-a", "batch_size": 20, "game": "wuwa", "term_anchor": True, "refine": False}
    try:
        manifest = create_manifest(input_path, options, [])
        with open(input_path, "wb") as changed:
            changed.write(b"new")
        try:
            validate_resume(manifest, input_path, options)
            assert False, "expected changed input to be rejected"
        except ValueError as error:
            assert "输入文件" in str(error)
    finally:
        os.unlink(input_path)


def test_round_one_rebase_reuses_only_exact_source_signature(tmp_path):
    from pipeline import main
    from pipeline.pipeline_manifest import create_manifest, set_batch

    old_input = tmp_path / "old.srt"
    new_input = tmp_path / "new.srt"
    old_input.write_text("old", encoding="utf-8")
    new_input.write_text("new", encoding="utf-8")
    options = {
        "model": "test", "batch_size": 2, "game": "wuwa",
        "term_anchor": True, "refine": False,
        "preserve_skipped": False,
    }
    old = create_manifest(str(old_input), {
        key: value for key, value in options.items()
        if key != "preserve_skipped"
    }, [
        {"id": 1, "start": "00:00:00,000", "end": "00:00:01,000",
         "original": "Same"},
        {"id": 2, "start": "00:00:02,000", "end": "00:00:03,000",
         "original": "中文"},
    ])
    set_batch(old, "0", "success", ids=[1, 2], results=[
        {"id": 1, "original": "Same", "translated": "相同"},
        {"id": 2, "original": "中文", "translated": "中文"},
    ])
    current = [
        Subtitle(1, "00:00:00,000", "00:00:01,000", "Same"),
        Subtitle(2, "00:00:02,000", "00:00:03,000", "English"),
    ]

    rebased, migrated = main._rebase_round_one_manifest(
        old, str(new_input), options, current, [current]
    )

    assert migrated == 1
    assert rebased["batches"]["0"]["state"] == "pending"
    assert rebased["batches"]["0"]["results"][0]["translated"] == "相同"


def test_incomplete_success_batch_is_only_partially_reusable():
    manifest = {"batches": {"0": {
        "state": "success", "ids": [1, 2], "results": [
            {"id": 1, "original": "Hello", "translated": "你好"},
            {"id": 2, "original": "World", "translated": ""},
        ],
    }}}

    assert successful_batch_results(manifest, "0") is None
    assert [item["id"] for item in partial_batch_results(manifest, "0")] == [1]


def test_manifest_defensively_replaces_untrusted_errors_and_absolute_paths(tmp_path):
    source = tmp_path / "private" / "input.srt"
    source.parent.mkdir()
    source.write_text("subtitle input", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    malicious = (
        "api_key=sk-live-manifest Authorization: Bearer bearer-manifest "
        "Cookie: session=private password=manifest-password "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )
    manifest = create_manifest(str(source), {"model": "test"}, [])
    manifest["artifacts"]["output"] = {
        "path": str(tmp_path / "private" / "output.srt"),
        "sha256": "abc",
    }

    set_batch(manifest, "0", "failed", error=malicious)
    save_manifest(str(manifest_path), manifest)

    encoded = manifest_path.read_text(encoding="utf-8")
    loaded = load_manifest(str(manifest_path))
    for secret in (
        "sk-live-manifest",
        "bearer-manifest",
        "session=private",
        "manifest-password",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
        str(tmp_path),
    ):
        assert secret not in encoded
    assert loaded["input"]["path"] == "input.srt"
    assert loaded["artifacts"]["output"]["path"] == "output.srt"
    assert loaded["batches"]["0"]["error_code"] == "batch_processing_error"


if __name__ == "__main__":
    test_manifest_roundtrip_and_success_result()
    test_resume_rejects_changed_input()
    print("[PASS] manifest tests")


def test_throttled_writer_coalesces_success_writes(tmp_path, monkeypatch):
    """S-2：success 状态按 flush_every 合并落盘，不再每批整份重写。"""
    import pipeline.pipeline_manifest as pm

    writes = {"count": 0}
    original_save = pm.save_manifest

    def counting_save(path, manifest):
        writes["count"] += 1
        return original_save(path, manifest)

    monkeypatch.setattr(pm, "save_manifest", counting_save)

    input_path = tmp_path / "input.srt"
    input_path.write_bytes(b"subtitle input")
    manifest_path = str(input_path) + ".manifest.json"
    options = {"model": "m", "batch_size": 20, "game": "wuwa",
               "term_anchor": True, "refine": False}
    manifest = create_manifest(str(input_path), options, [{"key": "1", "id": 1}])
    save_manifest(manifest_path, manifest)
    initial_writes = writes["count"]

    writer = pm.ThrottledManifestWriter(
        manifest_path, manifest, flush_every=8, flush_interval=3600.0,
    )
    for i in range(7):  # 少于 flush_every：不应触发任何写
        writer.set_batch(str(i), "success", ids=[i], results=[])
    assert writes["count"] == initial_writes
    assert writer.pending_batches == 7

    writer.set_batch("7", "success", ids=[7], results=[])  # 第 8 批：触发合并写
    assert writes["count"] == initial_writes + 1
    assert writer.pending_batches == 0

    # 落盘内容包含全部 8 批（合并写不丢批次）
    loaded = load_manifest(manifest_path)
    assert sorted(loaded["batches"]) == [str(i) for i in range(8)]
    assert all(
        batch["state"] == "success" for batch in loaded["batches"].values()
    )


def test_throttled_writer_flushes_failure_immediately(tmp_path):
    """失败/取消状态必须立即落盘（失败原因不能等节流窗口）。"""
    input_path = tmp_path / "input.srt"
    input_path.write_bytes(b"subtitle input")
    manifest_path = str(input_path) + ".manifest.json"
    options = {"model": "m", "batch_size": 20, "game": "wuwa",
               "term_anchor": True, "refine": False}
    manifest = create_manifest(str(input_path), options, [{"key": "1", "id": 1}])
    save_manifest(manifest_path, manifest)

    from pipeline.pipeline_manifest import ThrottledManifestWriter
    writer = ThrottledManifestWriter(
        manifest_path, manifest, flush_every=100, flush_interval=3600.0,
    )
    writer.set_batch("0", "running", ids=[1])
    assert "0" not in load_manifest(manifest_path)["batches"] or (
        load_manifest(manifest_path)["batches"]["0"]["state"] == "running"
    ) or writer.pending_batches == 1

    writer.set_batch(
        "0", "failed", ids=[1], error="x", error_code="api_error",
    )
    assert writer.pending_batches == 0, "failed 必须立即落盘"
    loaded = load_manifest(manifest_path)
    assert loaded["batches"]["0"]["state"] == "failed"
    assert loaded["batches"]["0"]["error_code"] == "api_error"


def test_throttled_writer_force_flush_persists_top_level_changes(tmp_path):
    """调用方改了 status/metrics 后用 flush(force=True) 强制落盘。"""
    input_path = tmp_path / "input.srt"
    input_path.write_bytes(b"subtitle input")
    manifest_path = str(input_path) + ".manifest.json"
    options = {"model": "m", "batch_size": 20, "game": "wuwa",
               "term_anchor": True, "refine": False}
    manifest = create_manifest(str(input_path), options, [])
    save_manifest(manifest_path, manifest)

    from pipeline.pipeline_manifest import ThrottledManifestWriter
    writer = ThrottledManifestWriter(
        manifest_path, manifest, flush_every=100, flush_interval=3600.0,
    )
    manifest["status"] = "cancelled"
    writer.flush()  # 无脏批次：普通 flush 不写
    assert load_manifest(manifest_path)["status"] == "running"
    writer.flush(force=True)
    assert load_manifest(manifest_path)["status"] == "cancelled"
