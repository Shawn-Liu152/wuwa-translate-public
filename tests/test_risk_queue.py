import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.candidates import EnglishCandidate, SourceCandidate
from pipeline.parser.srt_parser import Subtitle
from pipeline.risk_queue import (
    AUTOMATIC_REVIEW_REASONS, TRIAGE_ADVISORY_REASONS, build_risk_queue,
    triage_risk_item,
)


def test_conflict_and_number_mismatch_are_prioritized():
    candidates = [EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:03,000",
        capcut_en="Zani C6 does 200 damage", whisper_en="Zani C6 does 200 damage",
        source="capcut", flags=["text_conflict"],
    )]
    translated = [Subtitle(1, "00:00:00,000", "00:00:03,000", "赞妮C6造成100伤害")]
    risks = build_risk_queue(candidates, translated, {"Zani": "赞妮"})
    assert len(risks) == 1
    assert "text_conflict" in risks[0].reasons
    assert "number_mismatch" in risks[0].reasons
    assert risks[0].score >= 8


def test_clean_candidate_skips_round_two():
    candidates = [EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        capcut_en="Hello", whisper_en="Hello", source="capcut", flags=[],
    )]
    translated = [Subtitle(1, "00:00:00,000", "00:00:02,000", "你好")]
    assert build_risk_queue(candidates, translated) == []


def test_valid_number_words_semantic_markers_and_names_do_not_trigger():
    candidates = [EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:04,000",
        capcut_en="If Rover reaches 100 percent, use DPS.",
        whisper_en="If Rover reaches 100 percent use DPS",
        source="capcut", flags=[],
    )]
    translated = [Subtitle(
        1, "00:00:00,000", "00:00:04,000",
        "如果漂泊者达到百分百，就打DPS。",
    )]

    assert build_risk_queue(candidates, translated) == []


def test_untranslated_titlecase_proper_nouns_require_review():
    candidates = [
        EnglishCandidate(
            key="violet", start="00:00:00,000", end="00:00:02,000",
            capcut_en="She sounds just like Violet", source="capcut", flags=[],
        ),
        EnglishCandidate(
            key="wangchuan", start="00:00:02,000", end="00:00:04,000",
            capcut_en="The Wangchuan exhibition", source="capcut", flags=[],
        ),
    ]
    translated = [
        Subtitle(1, candidates[0].start, candidates[0].end, "她听起来像 Violet"),
        Subtitle(2, candidates[1].start, candidates[1].end, "Wangchuan 展览"),
    ]

    risks = build_risk_queue(candidates, translated)

    assert len(risks) == 2
    assert all("english_residue" in item.reasons for item in risks)
    assert all(item.review_required for item in risks)


def test_contextual_brand_preservation_is_manual_review_not_translation_failure():
    candidate = EnglishCandidate(
        key="sponsor-brand",
        start="00:00:00,000",
        end="00:00:03,000",
        capcut_en="This video is sponsored by Buff Buff.",
        whisper_en="This video is sponsored by Buff Buff.",
        source="capcut",
        flags=[],
    )
    translated = [Subtitle(
        1, candidate.start, candidate.end, "本视频由 Buff Buff 赞助。",
    )]

    risks = build_risk_queue(
        [candidate], translated, {"Buff": "增益"},
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["contextual_entity_preserved"]
    assert risks[0].contextual_entities == ["Buff Buff"]
    assert risks[0].review_required is True
    assert set(risks[0].reasons).isdisjoint(AUTOMATIC_REVIEW_REASONS)
    assert "english_residue" not in risks[0].reasons
    assert "term_mismatch" not in risks[0].reasons


def test_missing_secondary_source_alone_does_not_create_review_noise():
    candidates = [EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        capcut_en="A clean primary subtitle", whisper_en="",
        source="capcut", flags=["whisper_missing"],
    )]
    translated = [Subtitle(
        1, "00:00:00,000", "00:00:02,000", "干净的主字幕",
    )]

    assert build_risk_queue(candidates, translated) == []


def test_empty_and_pure_music_sources_never_enter_risk_queue():
    candidates = [
        EnglishCandidate(
            key="empty", start="00:00:00,000", end="00:00:00,010",
            capcut_en="", whisper_en="", source="whisper", flags=[],
        ),
        EnglishCandidate(
            key="punctuation", start="00:00:01,000", end="00:00:01,500",
            capcut_en="♪♪♪", whisper_en="", source="capcut", flags=[],
        ),
        EnglishCandidate(
            key="music", start="00:00:02,000", end="00:00:03,000",
            capcut_en="[Music]", whisper_en="", source="capcut", flags=[],
        ),
    ]
    translated = [
        Subtitle(1, item.start, item.end, "")
        for item in candidates
    ]

    assert build_risk_queue(
        candidates,
        translated,
        is_pure_music=lambda text: text.casefold() == "[music]",
    ) == []


def test_mixed_dialogue_and_music_is_still_reviewable():
    candidate = EnglishCandidate(
        key="mixed", start="00:00:00,000", end="00:00:02,000",
        capcut_en="Believe it [Music]", whisper_en="",
        source="capcut", flags=[],
    )
    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "")],
        is_pure_music=lambda text: False,
    )
    assert len(risks) == 1
    assert risks[0].reasons == ["empty_translation"]


def test_risk_queue_removes_inline_music_from_all_evidence():
    candidate = EnglishCandidate(
        key="mixed-clean", start="00:00:00,000", end="00:00:02,000",
        capcut_en="I have not seen [Music] this girl.",
        whisper_en="I have not seen (music) this girl.",
        source="capcut", flags=[],
    )

    def clean(text: str) -> str:
        return text.replace("[Music]", "").replace("(music)", "").replace(
            "[\u97f3\u4e50]", ""
        ).strip()

    risks = build_risk_queue(
        [candidate],
        [Subtitle(
            1, candidate.start, candidate.end,
            "\u6211\u6ca1\u89c1\u8fc7\u8fd9\u4e2a\u5973\u5b69\u3002[\u97f3\u4e50]",
        )],
        clean_music=clean,
    )
    assert len(risks) == 0


def test_weak_source_conflict_is_advisory_not_required_review():
    candidates = [EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:03,000",
        capcut_en="This looks incredible",
        whisper_en="It looks really incredible",
        source="capcut", flags=["text_conflict"],
    )]
    translated = [Subtitle(
        1, "00:00:00,000", "00:00:03,000", "这看起来太绝了",
    )]

    risks = build_risk_queue(candidates, translated)

    assert len(risks) == 1
    # 2026-08-10 审计：text_conflict 是 Source Truth 级风险（双源文本冲突
    # 意味着 canonical 可能选错），必须进自动 Reviewer，不再是纯 advisory。
    assert risks[0].severity == "medium"
    assert risks[0].review_required is True
    assert "text_conflict" in risks[0].reasons


def test_name_hint_is_strong_and_carries_context():
    candidates = [
        EnglishCandidate(
            key="before", start="00:00:00,000", end="00:00:01,000",
            capcut_en="Who is next?", source="capcut", flags=[],
        ),
        EnglishCandidate(
            key="risk", start="00:00:01,000", end="00:00:02,000",
            capcut_en="It is Chagli", source="capcut", flags=[],
        ),
        EnglishCandidate(
            key="after", start="00:00:02,000", end="00:00:03,000",
            capcut_en="She looks amazing", source="capcut", flags=[],
        ),
    ]
    translated = [
        Subtitle(1, candidates[0].start, candidates[0].end, "下一个是谁"),
        Subtitle(2, candidates[1].start, candidates[1].end, "是查格丽"),
        Subtitle(3, candidates[2].start, candidates[2].end, "她太惊艳了"),
    ]

    risks = build_risk_queue(
        candidates, translated, {"Chagli": "长离"}
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["term_mismatch"]
    assert risks[0].review_required is True
    assert risks[0].term_hints == {"Chagli": "长离"}
    assert risks[0].context_before == "Who is next?"
    assert risks[0].context_after == "She looks amazing"


def test_correct_person_name_still_requires_manual_confirmation():
    candidate = EnglishCandidate(
        key="yangyang",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="Yangyang is here",
        whisper_en="Yangyang is here",
        source="capcut",
        flags=[],
    )
    translated = [
        Subtitle(1, candidate.start, candidate.end, "秧秧来了")
    ]

    risks = build_risk_queue(
        [candidate],
        translated,
        {"Yangyang": "秧秧"},
        {"Yangyang": "秧秧"},
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["person_name_present"]
    assert risks[0].review_required is True
    assert risks[0].term_hints == {"Yangyang": "秧秧"}


def test_person_name_from_secondary_evidence_requires_manual_confirmation():
    """A correct secondary transcript must protect against a bad primary ASR name."""
    candidate = EnglishCandidate(
        key="denia-secondary",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="Denny is here",
        whisper_en="Denia is here",
        source="capcut",
        flags=["text_conflict"],
    )
    translated = [
        Subtitle(1, candidate.start, candidate.end, "丹尼来了")
    ]

    risks = build_risk_queue(
        [candidate],
        translated,
        {"Denia": "德尼娅"},
        {"Denia": "德尼娅"},
    )

    assert len(risks) == 1
    assert "person_name_present" in risks[0].reasons
    assert risks[0].review_required is True
    assert risks[0].term_hints["Denia"] == "德尼娅"
    # 2026-08-10 审计：text_conflict 进自动审查后，此 case 的 reasons
    # 与 AUTO 集合相交；但 person_name_present 本身仍是人工级原因。
    assert "person_name_present" not in AUTOMATIC_REVIEW_REASONS
    assert "text_conflict" in risks[0].reasons


def test_non_person_term_does_not_require_manual_confirmation_when_correct():
    candidate = EnglishCandidate(
        key="echo",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="This Echo is strong",
        whisper_en="This Echo is strong",
        source="capcut",
        flags=[],
    )
    translated = [
        Subtitle(1, candidate.start, candidate.end, "这个声骸很强")
    ]

    assert build_risk_queue(
        [candidate],
        translated,
        {"Echo": "声骸"},
        {},
    ) == []


def test_name_forgetting_speaker_fragment_requires_review():
    candidate = EnglishCandidate(
        key="forgotten-name",
        start="00:00:00,000",
        end="00:00:03,000",
        capcut_en=">> Swaye something. I don't actually remember.",
        source="capcut",
        flags=[],
    )
    translated = [
        Subtitle(
            1, candidate.start, candidate.end,
            "叫什么来着，我确实不记得了。",
        )
    ]

    risks = build_risk_queue([candidate], translated)

    assert len(risks) == 1
    assert risks[0].reasons == ["suspected_name"]
    assert risks[0].review_required is True


def test_conditional_wording_is_not_treated_as_strong_semantic_loss():
    candidate = EnglishCandidate(
        key="1", start="00:00:00,000", end="00:00:03,000",
        capcut_en="I wonder whether she knows",
        source="capcut", flags=[],
    )
    translated = [Subtitle(
        1, candidate.start, candidate.end, "我想知道她知不知道",
    )]

    assert build_risk_queue([candidate], translated) == []


def test_truncated_source_and_translation_require_round_two_review():
    candidate = EnglishCandidate(
        key="fragment",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="I don't even have a",
        source="capcut",
        flags=["whisper_missing"],
    )
    translated = [
        Subtitle(1, candidate.start, candidate.end, "我甚至都没有一个")
    ]

    risks = build_risk_queue([candidate], translated)

    assert len(risks) == 1
    assert "incomplete_fragment" in risks[0].reasons
    assert risks[0].review_required
    assert risks[0].needs_audio_evidence


def test_very_does_not_count_as_a_chinese_negation_marker():
    candidate = EnglishCandidate(
        key="negation",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="She is not strong",
        whisper_en="She is not strong",
        source="capcut",
        flags=[],
    )
    translated = [
        Subtitle(1, candidate.start, candidate.end, "她非常强")
    ]

    risks = build_risk_queue([candidate], translated)

    assert len(risks) == 1
    assert "negation_missing" in risks[0].reasons


def test_japanese_semantic_risks_use_japanese_source_rules():
    candidates = [
        SourceCandidate(
            key="ja-negation", start="00:00:00,000", end="00:00:02,000",
            primary_evidence="\u7d76\u5bfe\u306b\u8ca0\u3051\u306a\u3044",
            secondary_evidence="\u7d76\u5bfe\u306b\u8ca0\u3051\u306a\u3044",
            source_language="ja", flags=[],
        ),
        SourceCandidate(
            key="ja-condition", start="00:00:02,000", end="00:00:04,000",
            primary_evidence="\u3082\u3057\u52dd\u3066\u306a\u3051\u308c\u3070\u3001\u64a4\u9000\u3059\u308b",
            secondary_evidence="\u3082\u3057\u52dd\u3066\u306a\u3051\u308c\u3070\u3001\u64a4\u9000\u3059\u308b",
            source_language="ja", flags=[],
        ),
    ]
    translated = [
        Subtitle(1, candidates[0].start, candidates[0].end, "绝对会输"),
        Subtitle(2, candidates[1].start, candidates[1].end, "赢了就撤退"),
    ]

    risks = build_risk_queue(candidates, translated, source_language="ja")

    assert "negation_missing" in risks[0].reasons
    assert "condition_marker_missing" in risks[1].reasons
    assert all("english_residue" not in item.reasons for item in risks)


def test_correct_japanese_person_name_requires_human_review_only():
    candidate = SourceCandidate(
        key="ja-person", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="\u30ab\u30eb\u30c6\u30b8\u30a2\u304c\u6765\u305f",
        secondary_evidence="\u30ab\u30eb\u30c6\u30b8\u30a2\u304c\u6765\u305f",
        source_language="ja", flags=[],
    )
    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "卡提希娅来了")],
        {"\u30ab\u30eb\u30c6\u30b8\u30a2": "卡提希娅"},
        {"\u30ab\u30eb\u30c6\u30b8\u30a2": "卡提希娅"},
        source_language="ja",
    )

    assert risks[0].reasons == ["person_name_present"]
    assert risks[0].review_required is True
    assert set(risks[0].reasons).isdisjoint(AUTOMATIC_REVIEW_REASONS)


def test_corrected_japanese_name_still_requires_review_when_asr_name_is_mangled():
    candidate = SourceCandidate(
        key="ja-mangled-person", start="00:00:00,000", end="00:00:03,000",
        primary_evidence="今回は水水の編成です",
        secondary_evidence="今回は地水の編成です",
        source_language="ja", flags=["text_conflict"],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "这次介绍穗穗的配队")],
        person_glossary={"スイスイ": "穗穗", "穂穂": "穗穗"},
        source_language="ja",
    )

    assert "person_name_present" in risks[0].reasons
    assert risks[0].term_hints == {"スイスイ": "穗穗"}
    assert risks[0].review_required is True


def test_japanese_kana_left_in_chinese_output_is_blocking_risk():
    candidate = SourceCandidate(
        key="ja-kana", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="\u30ab\u30eb\u30c6\u30b8\u30a2\u306f\u5f37\u3044",
        secondary_evidence="\u30ab\u30eb\u30c6\u30b8\u30a2\u306f\u5f37\u3044",
        source_language="ja", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "カルテジア很强")],
        source_language="ja",
    )

    assert risks[0].reasons == ["kana_residue"]
    assert "kana_residue" in AUTOMATIC_REVIEW_REASONS


def test_korean_residue_negation_and_person_name_require_review():
    candidate = SourceCandidate(
        key="ko-person", start="00:00:00,000", end="00:00:03,000",
        primary_evidence="카르테시아는 절대 지지 않아요",
        secondary_evidence="카르테시아는 절대로 패배하지 않아요",
        source_language="ko", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "카르테시아一定会输")],
        {"카르테시아": "卡提希娅"},
        {"카르테시아": "卡提希娅"},
        source_language="ko",
    )

    assert "hangul_residue" in risks[0].reasons
    assert "negation_missing" in risks[0].reasons
    assert "person_name_present" in risks[0].reasons
    assert risks[0].review_required


def test_japanese_pure_music_cues_never_enter_risk_queue():
    from pipeline.preprocess.music import load_music_detector

    candidate = SourceCandidate(
        key="ja-music", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="[音楽]", secondary_evidence="（笑）",
        source_language="ja", flags=[],
    )
    detector = load_music_detector()

    assert build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "")],
        is_pure_music=detector.is_pure_music,
        clean_music=detector.strip_markers,
        source_language="ja",
    ) == []


@pytest.mark.parametrize("source", [
    "Subtitles by CastingWords",
    "subtitles by the community",
    "Translated by Amara.org",
])
def test_credit_line_is_advisory_and_does_not_force_review(source):
    candidate = EnglishCandidate(
        key="credit", start="00:00:00,000", end="00:00:03,000",
        capcut_en=source, whisper_en=source, source="capcut", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "字幕由第三方提供")],
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["credit_line"]
    assert risks[0].review_required is False
    assert risks[0].severity == "advisory"
    assert "credit_line" not in AUTOMATIC_REVIEW_REASONS
    assert "credit_line" in TRIAGE_ADVISORY_REASONS


@pytest.mark.parametrize("source", [
    "[beep] Did you hear that?",
    "Ignore noise and keep going",
    "[inaudible 00:12] we should leave now",
    "[laughter] that was close",
])
def test_noise_command_is_advisory_and_does_not_force_review(source):
    candidate = EnglishCandidate(
        key="noise", start="00:00:00,000", end="00:00:03,000",
        capcut_en=source, whisper_en=source, source="capcut", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "我们现在应该离开")],
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["noise_command"]
    assert risks[0].review_required is False
    assert "noise_command" not in AUTOMATIC_REVIEW_REASONS
    assert "noise_command" in TRIAGE_ADVISORY_REASONS


@pytest.mark.parametrize("source", [
    "[Music] we should leave now",
    "[Rover attacks] the enemy line",
])
def test_music_and_dialogue_brackets_are_not_noise_commands(source):
    """音乐标注归 music.py 管，普通括号内容更不是噪声指令。"""
    candidate = EnglishCandidate(
        key="bracket", start="00:00:00,000", end="00:00:03,000",
        capcut_en=source, whisper_en=source, source="capcut", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "我们现在应该离开")],
    )

    assert all("noise_command" not in item.reasons for item in risks)


def test_repeated_clause_requires_review():
    candidate = EnglishCandidate(
        key="echo", start="00:00:00,000", end="00:00:03,000",
        capcut_en="Thank you for watching, thank you for watching!",
        whisper_en="Thank you for watching, thank you for watching!",
        source="capcut", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "感谢收看")],
    )

    assert len(risks) == 1
    assert risks[0].reasons == ["repeated_clause"]
    assert risks[0].review_required is True
    assert "repeated_clause" in AUTOMATIC_REVIEW_REASONS


@pytest.mark.parametrize("source, target", [
    ("Very good, very good!", "非常好"),
    ("No no no, that is not it", "不不不，不是那样"),
    # 实测样本：真人反复喊话，词数够多但只有一个不同的词。
    ("wait wait wait wait wait wait wait wait", "等等等等"),
    ("Woah woah woah woah woah woah.", "哇哇哇哇哇哇。"),
])
def test_normal_spoken_repetition_is_not_a_repeated_clause(source, target):
    candidate = EnglishCandidate(
        key="spoken", start="00:00:00,000", end="00:00:02,000",
        capcut_en=source, whisper_en=source, source="capcut", flags=[],
    )

    assert build_risk_queue(
        [candidate], [Subtitle(1, candidate.start, candidate.end, target)],
    ) == []


def test_multi_word_echo_from_real_asr_output_is_a_repeated_clause():
    candidate = EnglishCandidate(
        key="echo-real", start="01:44:14,333", end="01:44:30,666",
        capcut_en="yours is gonna dashmas yours is gonna dashmas",
        whisper_en="yours is gonna dashmas yours is gonna dashmas",
        source="capcut", flags=[],
    )

    risks = build_risk_queue(
        [candidate],
        [Subtitle(1, candidate.start, candidate.end, "你要冲了")],
    )

    assert len(risks) == 1
    assert "repeated_clause" in risks[0].reasons


_DENSITY_FILE = [
    ("n1", "00:00:00,000", "00:00:04,000",
     "Rover deals a lot of damage in this fight", "漂泊者在这场战斗中伤害很高"),
    ("n2", "00:00:04,000", "00:00:08,000",
     "You should switch to the healer before the next wave", "下一波之前切换到治疗角色"),
    ("n3", "00:00:08,000", "00:00:12,000",
     "The boss enters its second phase at half health", "首领在半血时进入第二阶段"),
    ("sparse", "00:00:12,000", "00:00:20,000", "Hm", "嗯"),
    ("interjection", "00:00:20,000", "00:00:23,000", "Yeah.", "是啊。"),
]


def _build_density_queue():
    candidates = [
        EnglishCandidate(
            key=key, start=start, end=end,
            capcut_en=source, whisper_en=source, source="capcut", flags=[],
        )
        for key, start, end, source, _ in _DENSITY_FILE
    ]
    translated = [
        Subtitle(index, start, end, target)
        for index, (_, start, end, _, target) in enumerate(_DENSITY_FILE, start=1)
    ]
    return build_risk_queue(candidates, translated)


def test_low_info_density_flags_only_the_long_near_empty_cue():
    risks = _build_density_queue()

    assert [item.key for item in risks] == ["sparse"]
    assert risks[0].reasons == ["low_info_density"]
    assert risks[0].review_required is True
    assert "low_info_density" in AUTOMATIC_REVIEW_REASONS


def test_low_info_density_ignores_a_short_interjection():
    """3 秒的短应答密度也很低，但那是正常对白，不是 ASR 漏识别。"""
    risks = _build_density_queue()

    assert all(item.key != "interjection" for item in risks)


def test_advisory_asr_reasons_do_not_block_auto_triage():
    """advisory reason 若没进 TRIAGE_ADVISORY_REASONS，会把条目卡进人工队列。"""
    item = {
        "review_required": True,
        "reasons": ["reading_speed", "credit_line", "noise_command"],
    }

    assert triage_risk_item(item, whitelist={}) == (True, "advisory_only")


def test_content_level_reasons_still_block_auto_triage():
    item = {
        "review_required": True,
        "reasons": ["credit_line", "term_mismatch"],
    }

    assert triage_risk_item(item, whitelist={}) == (False, "")


if __name__ == "__main__":
    test_conflict_and_number_mismatch_are_prioritized()
    test_clean_candidate_skips_round_two()
    print("[PASS] risk queue tests")
