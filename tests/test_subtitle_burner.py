from pathlib import Path

from pipeline.subtitle_burner import (
    BLACK_OUTLINE_WHITE_2,
    BurnRequest,
    EncoderChoice,
    build_burn_command,
    check_burn_capability,
    style_for_resolution,
    write_ass,
)


def test_black_outline_white_2_ass_is_clean_and_resolution_aware(tmp_path):
    source = tmp_path / "final.zh.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,250\n你好\n世界\n",
        encoding="utf-8",
    )
    output = tmp_path / "subtitles.ass"

    style = style_for_resolution(
        1920,
        1080,
        font_name="Noto Sans CJK SC",
    )
    write_ass(source, output, style)

    rendered = output.read_text(encoding="utf-8")
    assert BLACK_OUTLINE_WHITE_2 == "black-outline-white-2-v1"
    assert "PlayResX: 1920" in rendered
    assert "PlayResY: 1080" in rendered
    assert "Style: BlackOutlineWhite2,Noto Sans CJK SC,65" in rendered
    assert "&H00FFFFFF" in rendered
    assert "&H00000000" in rendered
    assert ",4.3,0.0,2,60," in rendered
    assert "Dialogue: 0,0:00:01.00,0:00:03.25,BlackOutlineWhite2,,0,0,0,,你好\\N世界" in rendered


def test_build_burn_command_uses_only_controlled_relative_names(tmp_path):
    request = BurnRequest(
        video_path=tmp_path / "private source.mp4",
        subtitle_path=tmp_path / "final.zh.srt",
        output_path=tmp_path / "final.zh.burned.mp4",
    )
    encoder = EncoderChoice(name="libx264", hardware=False)

    command = build_burn_command(
        request,
        encoder,
        tmp_path / "burn-work",
    )

    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == "input.mp4"
    assert command[command.index("-vf") + 1] == "ass=subtitles.ass:fontsdir=fonts"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-progress") + 1] == "pipe:1"
    assert command[-1] == "output.tmp.mp4"
    assert not any("private source" in part for part in command)


def test_missing_ffmpeg_returns_stable_public_capability(monkeypatch):
    monkeypatch.setattr("pipeline.subtitle_burner.shutil.which", lambda _name: None)

    capability = check_burn_capability()

    assert capability.available is False
    assert capability.error_code == "ffmpeg_missing"
    assert capability.message == "未安装 FFmpeg，无法生成带字幕视频"
    assert capability.ffmpeg_path is None
    assert capability.font is None
