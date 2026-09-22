import json
import os

from pipeline import long_video, main


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "ko", "reaction.ko.srt",
)


def test_korean_pipeline_preserves_hangul_in_dry_run(tmp_path):
    output = tmp_path / "round1.zh.srt"

    status = main.run_pipeline(
        FIXTURE, str(output), model="test", api_key="", batch_size=2,
        game="wuwa", dry_run=True, source_language="ko",
        target_language="zh-CN", data_dir=DATA_DIR,
    )

    assert status == 0
    assert "카르테시아" in output.read_text(encoding="utf-8-sig")


def test_korean_fixture_runs_complete_two_round_orchestration(tmp_path):
    secondary = tmp_path / "secondary.ko.srt"
    secondary.write_text("", encoding="utf-8")
    output = tmp_path / "fixture.zh.srt"

    status = long_video.run_long_video(
        FIXTURE, str(secondary), str(output), model="test", api_key="",
        batch_size=2, game="wuwa", resume=False, dry_run=True,
        source_language="ko", target_language="zh-CN", data_dir=DATA_DIR,
    )

    generated = json.loads(
        (tmp_path / "fixture.zh.risk.generated.json").read_text(encoding="utf-8")
    )
    metrics = json.loads(
        (tmp_path / "fixture.zh.pipeline.metrics.json").read_text(encoding="utf-8")
    )
    assert status == 0
    assert output.is_file()
    assert (tmp_path / "fixture.zh.ko.union.final.srt").is_file()
    assert (tmp_path / "fixture.zh.display-map.json").is_file()
    assert all(
        item["source_language"] == "ko"
        for item in generated.get("items", [])
    )
    assert not any(
        item.get("source_text") in {"[음악]", "(음악)"}
        for item in generated.get("items", [])
    )
    assert metrics["quality"]["display_cues"] >= 1
