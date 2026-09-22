import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.parser.srt_parser import Subtitle, save_srt
from pipeline.source_quality import assess_english_srt, assess_source_srt


def test_good_english_reference_is_usable(tmp_path):
    path = tmp_path / "good.en.srt"
    save_srt(str(path), [
        Subtitle(index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
                 f"English subtitle line number {index}")
        for index in range(1, 11)
    ])

    quality = assess_english_srt(path)

    assert quality.usable
    assert quality.score >= 60
    assert quality.english_ratio == 1.0


def test_non_english_reference_triggers_fallback(tmp_path):
    path = tmp_path / "wrong.en.srt"
    save_srt(str(path), [
        Subtitle(index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
                 f"这是第{index}条中文字幕")
        for index in range(1, 11)
    ])

    quality = assess_english_srt(path)

    assert not quality.usable
    assert "not_english" in quality.reasons


def test_rolling_micro_cues_request_an_independent_secondary_source(tmp_path):
    path = tmp_path / "rolling.en.srt"
    subtitles = [
        Subtitle(
            index,
            f"00:00:{index:02d},000",
            f"00:00:{index:02d},800",
            f"Complete English subtitle line {index}.",
        )
        for index in range(1, 10)
    ]
    subtitles.append(Subtitle(
        10,
        "00:00:10,000",
        "00:00:10,010",
        "greatness.",
    ))
    save_srt(str(path), subtitles)

    quality = assess_english_srt(path)

    assert quality.usable
    assert quality.needs_secondary
    assert quality.micro_cue_ratio == 0.1
    assert "rolling_fragments" in quality.reasons


def test_adaptive_reference_uses_whisper_as_optional_evidence_for_fragments():
    from web.jobs import _whisper_reference_plan

    class FragmentedQuality:
        usable = True
        needs_secondary = True

    should_run, required = _whisper_reference_plan(
        "adaptive", FragmentedQuality()
    )

    assert should_run
    assert not required


def test_readable_text_with_broken_display_durations_requests_timing_reference(
        tmp_path):
    path = tmp_path / "stretched.en.srt"
    subtitles = [
        Subtitle(
            index,
            f"00:00:{index * 2:02d},000",
            f"00:00:{index * 2 + (15 if index in {4, 8} else 1):02d},000",
            f"Complete English subtitle sentence number {index}.",
        )
        for index in range(1, 11)
    ]
    save_srt(str(path), subtitles)

    quality = assess_english_srt(path)

    assert quality.usable
    assert quality.needs_secondary
    assert quality.needs_timing_alignment
    assert quality.long_cue_ratio == 0.2
    assert "timing_anomalies" in quality.reasons


def test_japanese_reference_uses_language_aware_quality_without_latin_ratio(
        tmp_path):
    path = tmp_path / "good.ja.srt"
    save_srt(str(path), [
        Subtitle(
            index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
            f"第{index}話のカルテジアさん、本当にすごいね。",
        )
        for index in range(1, 11)
    ])

    quality = assess_source_srt(path, source_language="ja")

    assert quality.usable
    assert quality.source_language == "ja"
    assert "not_japanese" not in quality.reasons


def test_short_japanese_kanji_name_is_not_rejected_as_wrong_language(tmp_path):
    path = tmp_path / "kanji.ja.srt"
    save_srt(str(path), [
        Subtitle(index, f"00:00:{index:02d},000", f"00:00:{index:02d},800", "長離")
        for index in range(1, 11)
    ])

    quality = assess_source_srt(path, source_language="ja")

    assert quality.usable
    assert "not_japanese" not in quality.reasons


def test_japanese_reference_rejects_latin_gibberish_from_wrong_language_asr(
    tmp_path,
):
    path = tmp_path / "wrong-language.ja.srt"
    save_srt(str(path), [
        Subtitle(
            index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
            f"allthenext-seidlimo-isely-{index}",
        )
        for index in range(1, 11)
    ])

    quality = assess_source_srt(path, source_language="ja")

    assert not quality.usable
    assert "not_japanese" in quality.reasons


def test_korean_reference_accepts_hangul_and_rejects_english(tmp_path):
    korean = tmp_path / "good.ko.srt"
    save_srt(str(korean), [
        Subtitle(
            index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
            f"제{index}화의 카르테시아는 정말 대단하네요.",
        )
        for index in range(1, 11)
    ])
    english = tmp_path / "wrong.ko.srt"
    save_srt(str(english), [
        Subtitle(
            index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
            f"This is English subtitle number {index}.",
        )
        for index in range(1, 11)
    ])

    accepted = assess_source_srt(korean, source_language="ko")
    rejected = assess_source_srt(english, source_language="ko")

    assert accepted.usable
    assert accepted.language_ratio >= 0.75
    assert not rejected.usable
    assert "not_korean" in rejected.reasons


def test_source_quality_rejects_dense_repeated_short_cues(tmp_path):
    path = tmp_path / "rolling.ko.srt"
    save_srt(str(path), [
        Subtitle(
            index, f"00:00:{index // 2:02d},{(index % 2) * 500:03d}",
            f"00:00:{index // 2:02d},{(index % 2) * 500 + 400:03d}",
            "같은 자막입니다",
        )
        for index in range(1, 11)
    ])

    quality = assess_source_srt(path, source_language="ko")

    assert quality.short_cue_ratio == 1.0
    assert "rapid_short_cues" in quality.reasons
    assert "hallucination_loop" in quality.reasons
    assert not quality.usable


def test_source_quality_explains_missing_title_entities_without_guessing(tmp_path):
    path = tmp_path / "source.ko.srt"
    save_srt(str(path), [
        Subtitle(
            index, f"00:00:{index:02d},000", f"00:00:{index:02d},800",
            f"오늘 장면을 자세히 살펴보겠습니다 {index}",
        )
        for index in range(1, 11)
    ])

    quality = assess_source_srt(
        path, source_language="ko", expected_terms=["카르테시아", "피비"],
    )

    assert quality.entity_match_ratio == 0.0
    assert "title_entities_missing" in quality.reasons
    assert quality.needs_secondary
