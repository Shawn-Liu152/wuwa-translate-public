"""
Japanese Filter 测试

测试点：
- 平假名检测/删除
- 片假名检测/删除
- 日英混排保留英文
- 纯英文文本零误删
- 多行文本按行处理
- 全角片假名标点处理
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import long_video, main
from pipeline.parser.srt_parser import Subtitle
from pipeline.preprocess.japanese import has_japanese, remove_japanese
from pipeline.translate.llm import TranslateResult


def test_has_japanese_hiragana():
    """平假名检测"""
    assert has_japanese("こんにちは") is True
    assert has_japanese("これは日本語です") is True


def test_has_japanese_katakana():
    """片假名检测"""
    assert has_japanese("コンニチハ") is True
    assert has_japanese("カタカナ") is True


def test_has_japanese_false():
    """纯英文/中文不误判"""
    assert has_japanese("Hello World") is False
    assert has_japanese("你好世界") is False
    assert has_japanese("Rover, let's go!") is False
    assert has_japanese("") is False


def test_remove_pure_japanese():
    """纯日文假名行 → 完全删除"""
    assert remove_japanese("こんにちは") == ""
    assert remove_japanese("コンニチハ") == ""
    # 含汉字的日语：假名被删除，汉字保留（汉字不在过滤范围内，后续由中文翻译处理）
    assert remove_japanese("これは日本語です") == "日本語"


def test_remove_mixed_en_jp():
    """日英混排 → 删除日文假名，保留英文"""
    assert remove_japanese("Hello こんにちは") == "Hello"
    assert remove_japanese("こんにちは Hello") == "Hello"
    assert remove_japanese("Rover です") == "Rover"


def test_preserve_pure_english():
    """纯英文不误删"""
    assert remove_japanese("Hello World") == "Hello World"
    assert remove_japanese("Rover, let's go!") == "Rover, let's go!"
    assert remove_japanese("Changli said something.") == "Changli said something."


def test_multiline():
    """多行文本：日文行删除，英文行保留"""
    text = "Hello\nこんにちは\nWorld\nコンニチハ"
    result = remove_japanese(text)
    # 纯假名行被清空，英文行保留，日汉字保留
    assert "Hello" in result
    assert "World" in result
    assert "こんにちは" not in result
    assert "コンニチハ" not in result


def test_empty_result():
    """全部是日文 → 返回空字符串"""
    assert remove_japanese("こんにちは\nわーい") == ""


def test_preserve_chinese():
    """中文不被误删"""
    assert "你好" in remove_japanese("你好")
    assert remove_japanese("你好 世界") == "你好 世界"


def test_mixed_en_jp_multiline():
    """日英混排名场面"""
    text = "Rover!\n頑張って\nLet's go!"
    result = remove_japanese(text)
    assert "Rover!" in result
    assert "Let's go!" in result
    assert "頑張って" not in result


def test_japanese_pipeline_preserves_japanese_reaction_text(tmp_path, monkeypatch):
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    output = tmp_path / "final.zh.srt"

    monkeypatch.setattr(
        main,
        "remove_japanese",
        lambda _text: (_ for _ in ()).throw(
            AssertionError("Japanese task must not remove Japanese text"),
        ),
    )
    monkeypatch.setattr(
        main,
        "remove_non_english",
        lambda _text: (_ for _ in ()).throw(
            AssertionError("Japanese task must not use English-only filtering"),
        ),
    )

    status = main.run_pipeline(
        fixture,
        str(output),
        model="test",
        api_key="",
        batch_size=2,
        game="wuwa",
        dry_run=True,
        source_language="ja",
        target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    )

    assert status == 0
    assert "カルテジア" in output.read_text(encoding="utf-8-sig")


def test_japanese_pipeline_skips_english_alias_rules(tmp_path, monkeypatch):
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    monkeypatch.setattr(main.Config, "ENABLE_ALIAS", True)
    monkeypatch.setattr(
        main,
        "load_alias_fixer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Japanese source must not use English alias rules"),
        ),
    )

    assert main.run_pipeline(
        fixture,
        str(tmp_path / "final.zh.srt"),
        model="test", api_key="", batch_size=2, game="wuwa", dry_run=True,
        source_language="ja", target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    ) == 0


def test_japanese_pipeline_passes_language_to_youtube_dedupe(tmp_path, monkeypatch):
    from pipeline.preprocess import dedupe as dedupe_module

    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    received = []

    def fake_dedupe(subtitles, *, source_language):
        received.append(source_language)
        return subtitles

    monkeypatch.setattr(dedupe_module, "dedupe_youtube_subs", fake_dedupe)

    assert main.run_pipeline(
        fixture,
        str(tmp_path / "final.zh.srt"),
        model="test", api_key="", batch_size=2, game="wuwa", dry_run=True,
        dedupe=True, source_language="ja", target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    ) == 0
    assert received == ["ja"]


def test_japanese_sentence_boundary_keeps_no_space_clauses_together():
    subtitles = [
        Subtitle(1, "00:00:00,000", "00:00:01,000", "これは本当に"),
        Subtitle(2, "00:00:01,000", "00:00:02,000", "すごいと"),
        Subtitle(3, "00:00:02,000", "00:00:03,000", "思う。"),
    ]

    batches = main.build_adaptive_batches(subtitles, target_size=2)

    assert [[item.id for item in batch] for batch in batches] == [[1, 2, 3]]


def test_japanese_pipeline_locks_title_character_names_even_when_asr_mangles_them(
    tmp_path, monkeypatch,
):
    source = tmp_path / "mangled.ja.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n今回は次世代林檎です\n",
        encoding="utf-8",
    )
    observed = {}

    class Glossary:
        def extract_from_text(self, text, game="wuwa", source_language="ja"):
            result = {}
            if "穂穂" in text:
                result["穂穂"] = "穗穗"
            if "千咲" in text:
                result["千咲"] = "千咲"
            return result

    class Translator:
        def translate(self, batch, **kwargs):
            observed.update(kwargs.get("global_context") or {})
            return [
                TranslateResult(item.id, item.text, "这是角色组合")
                for item in batch
            ]

    monkeypatch.setattr(main, "load_glossary_db", lambda *_args: Glossary())
    monkeypatch.setattr(main, "create_translator", lambda **_kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(tmp_path / "final.zh.srt"),
        model="test", api_key="key", batch_size=8, game="wuwa",
        dedupe=False, source_language="ja", target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
        video_context={"title": "【鸣潮】穂穂×千咲最强组合"},
    ) == 0
    assert observed["known_terms"] == {"穂穂": "穗穗", "千咲": "千咲"}


def test_japanese_fixture_runs_complete_two_round_orchestration(tmp_path):
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    secondary = tmp_path / "secondary.ja.srt"
    secondary.write_text("", encoding="utf-8")
    output = tmp_path / "fixture.zh.srt"

    status = long_video.run_long_video(
        fixture,
        str(secondary),
        str(output),
        model="test",
        api_key="",
        batch_size=2,
        game="wuwa",
        resume=False,
        dry_run=True,
        source_language="ja",
        target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    )

    generated = json.loads((tmp_path / "fixture.zh.risk.generated.json").read_text(
        encoding="utf-8"
    ))
    assert status == 0
    assert output.is_file()
    assert (tmp_path / "fixture.zh.ja.union.final.srt").is_file()
    assert all(
        item["source_language"] == "ja"
        for item in generated.get("items", [])
    )
    assert any(
        "person_name_present" in item.get("reasons", [])
        and item.get("term_hints", {}).get("カルテジア") == "卡提希娅"
        for item in generated.get("items", [])
    )
    assert not any(
        item.get("source_text") in {"[音楽]", "（音楽）"}
        for item in generated.get("items", [])
    )


def test_japanese_local_risk_whisper_receives_task_language(tmp_path, monkeypatch):
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    received = {}

    def fake_collect(*_args, **kwargs):
        received.update(kwargs)

    monkeypatch.setattr(long_video, "_collect_local_evidence", fake_collect)
    status = long_video.run_long_video(
        fixture,
        fixture,
        str(tmp_path / "fixture.zh.srt"),
        model="test",
        api_key="",
        batch_size=2,
        game="wuwa",
        resume=False,
        dry_run=True,
        local_whisper=True,
        video=str(tmp_path / "video.mp4"),
        source_language="ja",
        target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    )

    assert status == 0
    assert received["source_language"] == "ja"


def test_japanese_quality_pipeline_creates_missing_output_directory(tmp_path):
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "ja", "reaction.ja.srt",
    )
    output = tmp_path / "new-output" / "fixture.zh.srt"

    status = long_video.run_long_video(
        fixture,
        fixture,
        str(output),
        model="test",
        api_key="",
        batch_size=2,
        game="wuwa",
        resume=False,
        dry_run=True,
        source_language="ja",
        target_language="zh-CN",
        data_dir=os.path.join(os.path.dirname(__file__), "..", "data"),
    )

    assert status == 0
    assert output.is_file()
    assert (tmp_path / "new-output" / "fixture.zh.ja.union.final.srt").is_file()


if __name__ == "__main__":
    print("=" * 60)
    print("Japanese Filter 测试套件")
    print("=" * 60)

    tests = [
        ("平假名检测", test_has_japanese_hiragana),
        ("片假名检测", test_has_japanese_katakana),
        ("英文不误判", test_has_japanese_false),
        ("纯日文删除", test_remove_pure_japanese),
        ("日英混排保留", test_remove_mixed_en_jp),
        ("纯英文保留", test_preserve_pure_english),
        ("多行文本", test_multiline),
        ("全日文结果", test_empty_result),
        ("中文保留", test_preserve_chinese),
        ("日英混排多行", test_mixed_en_jp_multiline),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
