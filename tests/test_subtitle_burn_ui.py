from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_translation_flow_has_one_optional_black_outline_video_switch():
    script = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "完成后生成带字幕视频" in script
    assert "黑框白字2" in script
    assert 'name="burn_after_translation"' in script
    assert "renderBurnedVideoExport" in script
    assert "/exports/burned-video" in script
    assert "/exports/burned-video/cancel" in script
    assert "剪映花字模式" not in script


def test_burned_video_uses_existing_artifact_download_and_cache_pins():
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    api_tests = (ROOT / "tests" / "test_web_api.py").read_text(encoding="utf-8")

    assert "final.zh.burned.mp4" in script
    assert "下载带字幕视频" in script
    assert "真正待处理 0" in page
    assert "自动处理完成的条目不会再次拦截视频生成" in page
    assert "app.css?v=20260923-three-ux-css27" in page
    assert "app.js?v=20260923-three-ux-v33" in page
    assert "app.css?v=20260923-three-ux-css27" in api_tests
    assert "app.js?v=20260923-three-ux-v33" in api_tests
