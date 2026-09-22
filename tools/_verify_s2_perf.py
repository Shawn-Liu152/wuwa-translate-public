"""S-2 性能验证：节流写入 vs 逐批整份重写（2000 条 / 63 批规模）。

跑法：.venv python tools/_verify_s2_perf.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.pipeline_manifest import (
    ThrottledManifestWriter,
    create_manifest,
    save_manifest,
    set_batch,
)


def make_entries(n):
    return [
        {
            "key": f"{i}|00:00:{i % 60:02d},000|00:00:{(i % 60) + 1:02d},000",
            "id": i,
            "start": f"00:{i // 60:02d}:{i % 60:02d},000",
            "end": f"00:{i // 60:02d}:{(i % 60) + 1:02d},000",
            "original": f"subtitle line number {i} with some content",
        }
        for i in range(n)
    ]


def results_for(batch_ids):
    return [
        {
            "id": i,
            "original": f"subtitle line number {i} with some content",
            "translated": f"第 {i} 行译文，内容稍长一些以接近真实体积",
            "tokens_in": 52,
            "tokens_out": 38,
            "cost": 0.0001,
            "cached": False,
        }
        for i in batch_ids
    ]


def run(mode: str, batches: int, batch_ids: list[list[int]]):
    fd, input_path = tempfile.mkstemp(suffix=".srt")
    os.close(fd)
    with open(input_path, "wb") as f:
        f.write(b"perf input")
    manifest_path = input_path + ".manifest.json"
    options = {"model": "m", "batch_size": 32, "game": "wuwa",
               "term_anchor": True, "refine": False}
    manifest = create_manifest(input_path, options, make_entries(2000))
    save_manifest(manifest_path, manifest)

    t0 = time.perf_counter()
    if mode == "old":
        for bid, ids in enumerate(batch_ids):
            set_batch(
                manifest, str(bid), "success", ids=ids,
                results=results_for(ids),
                metrics={"tokens_in": 1600, "tokens_out": 1200,
                         "cost": 0.001, "elapsed_seconds": 10.0,
                         "glossary_terms": 302, "response_attempts": 1},
            )
            save_manifest(manifest_path, manifest)
    else:
        writer = ThrottledManifestWriter(
            manifest_path, manifest, flush_every=8, flush_interval=30.0,
        )
        for bid, ids in enumerate(batch_ids):
            writer.set_batch(
                str(bid), "success", ids=ids,
                results=results_for(ids),
                metrics={"tokens_in": 1600, "tokens_out": 1200,
                         "cost": 0.001, "elapsed_seconds": 10.0,
                         "glossary_terms": 302, "response_attempts": 1},
            )
        writer.flush(force=True)
    elapsed = time.perf_counter() - t0

    with open(manifest_path, encoding="utf-8") as f:
        loaded = json.load(f)
    ok = all(
        b["state"] == "success" for b in loaded["batches"].values()
    ) and len(loaded["batches"]) == batches
    for p in (input_path, manifest_path):
        os.remove(p)
    return elapsed, ok


if __name__ == "__main__":
    batch_ids = [list(range(i * 32, min((i + 1) * 32, 2000))) for i in range(63)]
    old_t, old_ok = run("old", 63, batch_ids)
    new_t, new_ok = run("new", 63, batch_ids)
    print(f"old (per-batch full save): {old_t * 1000:.1f} ms  ok={old_ok}")
    print(f"new (throttled writer):    {new_t * 1000:.1f} ms  ok={new_ok}")
    print(f"reduction: {(1 - new_t / old_t) * 100:.1f}%")
