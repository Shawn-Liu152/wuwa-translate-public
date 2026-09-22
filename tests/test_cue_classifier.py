import json
from pathlib import Path
import shutil

from pipeline import long_video
from pipeline.parser.srt_parser import Subtitle, parse_srt, save_srt
from pipeline.preprocess.cue_classifier import CueClass, classify_cue
from pipeline.translate.llm import TranslateResult


DATA_DIR = str(Path(__file__).resolve().parents[1] / "data")


def test_classify_already_chinese():
    assert classify_cue("旅行者，欢迎回来") == CueClass.already_zh


def test_classify_english_reaction():
    assert (
        classify_cue("Oh my god, this character is insane")
        == CueClass.english_reaction
    )


def test_classify_music_sfx():
    assert classify_cue("[Music]") == CueClass.music_sfx


def test_classify_mixed_official_chinese_and_reaction():
    assert classify_cue("这个BOSS太强了 let's go") == CueClass.mixed


def test_classify_empty_and_punctuation_as_unknown():
    assert classify_cue("") == CueClass.unknown
    assert classify_cue("...") == CueClass.unknown


def test_classify_bracket_sfx_only_is_music_sfx():
    """[screaming] 类英文舞台指示此前被当英语送去翻译（2026-09-06 实测：
    源里 10 条 <200ms 的 SFX 碎 cue 全部进了 Round 1，其中一条 10ms
    闪帧 cue 连同占位符进成品）。整条只剩括号标注 → music_sfx。"""
    assert classify_cue("[screaming]") == CueClass.music_sfx
    assert classify_cue("[crying] [laughter]") == CueClass.music_sfx
    assert classify_cue(">> [applause]") == CueClass.music_sfx


def test_classify_bracket_marker_with_speech_stays_reaction():
    """句中括号标注剥除后仍有口语 → 正常按比例分类，不误杀。"""
    assert classify_cue("[laughter] What") == CueClass.english_reaction
    assert (
        classify_cue("[screaming] dancing Holy crap")
        == CueClass.english_reaction
    )


def test_long_video_bypasses_chinese_and_manual_review_cues(
    tmp_path, monkeypatch,
):
    isolated_data = tmp_path / "data"
    shutil.copytree(DATA_DIR, isolated_data)
    primary = tmp_path / "primary.srt"
    secondary = tmp_path / "secondary.srt"
    output = tmp_path / "final.zh.srt"
    source_subtitles = [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "旅行者，欢迎回来"),
        Subtitle(
            2, "00:00:03,000", "00:00:05,000",
            "Oh my god, this character is insane",
        ),
        Subtitle(
            3, "00:00:06,000", "00:00:08,000",
            "这个BOSS太强了 let's go",
        ),
        Subtitle(4, "00:00:09,000", "00:00:11,000", "[Applause]"),
    ]
    save_srt(str(primary), source_subtitles)
    save_srt(str(secondary), source_subtitles)

    api_inputs = []

    class Translator:
        def translate(self, subtitles, **_kwargs):
            api_inputs.extend(subtitle.text for subtitle in subtitles)
            return [
                TranslateResult(
                    id=subtitle.id,
                    original=subtitle.text,
                    translated="这个角色也太离谱了",
                )
                for subtitle in subtitles
            ]

    monkeypatch.setattr(
        "pipeline.main.create_translator", lambda **_kwargs: Translator(),
    )

    round_two_inputs = []

    def fake_round_two(items, *_args, **_kwargs):
        round_two_inputs.extend(items)
        return {}

    monkeypatch.setattr(long_video, "_round_two", fake_round_two)

    assert long_video.run_long_video(
        str(primary),
        str(secondary),
        str(output),
        model="test",
        api_key="test",
        batch_size=8,
        game="wuwa",
        resume=False,
        data_dir=str(isolated_data),
    ) == 0

    # 1b：MIXED cue 纳入翻译（此前整句直通，实测是成片中英混杂的主因之一；
    # 风旗保持不变，仍进人工审校）。union 文本先经实体归一化（BOSS→Boss），
    # 再经 en 链路既有的 remove_japanese 剥除混入汉字——模型至少翻译剩余
    # 口语部分，而不是整句直通。
    assert api_inputs == [
        "Oh my god, this character is insane",
        "Boss let's go",
    ]
    round1 = {
        subtitle.id: subtitle.text
        for subtitle in parse_srt(str(tmp_path / "final.round1.zh.srt"))
    }
    assert round1[1] == "旅行者，欢迎回来"
    assert round1[2] == "这个角色也太离谱了"
    assert round1[3] == "这个角色也太离谱了"
    assert round1[4] == ""

    risks = json.loads(
        (tmp_path / "final.zh.risk.generated.json").read_text(encoding="utf-8")
    )["items"]
    mixed = next(item for item in risks if item["subtitle_id"] == 3)
    assert mixed["reasons"] == ["cue_classification_mixed"]
    assert mixed["review_required"] is True
    # 纯音乐/SFX 已在 Source Truth 阶段判定，不得因为括号内的 Title Case
    # 单词被 Entity Guard 重新加入付费 Round 2。
    assert round_two_inputs == []
