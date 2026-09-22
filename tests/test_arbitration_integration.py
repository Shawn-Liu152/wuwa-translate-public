"""阶段 C 集成测试：仲裁接入 union + 风险队列（不破坏 en 链路）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.candidates import SourceCandidate
from pipeline.long_video import (
    _apply_accepted_reconstructions, _merge_arbitration_risks, _write_union,
)
from pipeline.preprocess.source_arbitration import (
    ArbitratedUnit, arbitrate_candidates,
)
from pipeline.parser.srt_parser import Subtitle

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _candidate(key, primary, secondary, lang="ko"):
    return SourceCandidate(
        key=key, start="00:00:00,000", end="00:00:02,000",
        primary_evidence=primary, secondary_evidence=secondary,
        primary_source="youtube", secondary_source="whisper",
        source_language=lang, primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )


def _subtitle(idx, start, end, text):
    return Subtitle(idx, start, end, text)


# ---------- 风险队列接入 ----------

def test_merge_arbitration_risks_adds_unresolved():
    """unresolved（两源冲突无证据）→ 追加 source_unresolved 风险（review_required）。"""
    candidates = [_candidate(
        "1", "아무튼 온 나이 너희가 무너질 거야", "카세차한다 뭐야",
    )]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "反正你们要完蛋")]
    risks = _merge_arbitration_risks([], candidates, translated, DATA_DIR, "ko")
    assert any(
        "source_unresolved" in item.reasons for item in risks
    )
    assert all(item.review_required for item in risks)


def test_merge_arbitration_risks_adds_unknown_entity():
    """canonical 含未知专名（메카우터）→ 追加 unknown_entity 风险。"""
    candidates = [_candidate(
        "1", "이번 작동에서 메카우터는", "",
    )]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "这次作战中，梅卡乌特")]
    risks = _merge_arbitration_risks([], candidates, translated, DATA_DIR, "ko")
    assert any(
        "unknown_entity:" in r for item in risks for r in item.reasons
    )


def test_merge_arbitration_risks_skips_clean_units():
    """两源一致且无未知专名 → 不追加仲裁风险。"""
    candidates = [_candidate(
        "1", "진짜 게이트를 열었네", "진짜 게이트를 열었네",
    )]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "真的把门打开了")]
    risks = _merge_arbitration_risks([], candidates, translated, DATA_DIR, "ko")
    assert risks == []


def test_merge_arbitration_risks_skips_pure_non_speech_units():
    """Pure SFX has no translation ID and must not become blocking ID 0."""
    from pipeline.preprocess.music import load_music_detector

    candidates = [_candidate(
        "sfx", "[叫び声]\n[笑い]", "", lang="ja",
    )]
    detector = load_music_detector()

    risks = _merge_arbitration_risks(
        [], candidates, [], DATA_DIR, "ja",
        is_pure_music=detector.is_pure_music,
    )

    assert risks == []


def test_merge_arbitration_risks_english_untouched():
    """en 不进入仲裁 → 原风险原样返回。"""
    candidates = [_candidate(
        "1", "Hello there", "Hello there", lang="en",
    )]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "你好")]
    risks = _merge_arbitration_risks([], candidates, translated, DATA_DIR, "en")
    assert risks == []


# ---------- source_text canonical 同步 ----------

def test_merge_arbitration_risks_updates_source_text_to_canonical():
    """Regular risk items must use canonical_source_text after arbitration merge.

    Regression test for the 99号句 case: build_risk_queue sets source_text
    to preferred_source_text (primary evidence for ja/ko), but after
    arbitration, the canonical may differ. _merge_arbitration_risks must
    update existing risk items' source_text to match the canonical so the
    Round 2 Reviewer compares Round 1 against the correct source.
    """
    from pipeline.risk_queue import RiskItem

    primary_text = "소용 없어요."
    secondary_text = "그만해요."
    candidates = [_candidate("1", primary_text, secondary_text)]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "住手！")]

    # Simulate what build_risk_queue creates: source_text = primary evidence
    regular_risk = RiskItem(
        key="1", subtitle_id=1,
        start="00:00:00,000", end="00:00:02,000",
        score=6, reasons=["negation_missing"],
        english=primary_text,
        capcut_en=primary_text,
        whisper_en=secondary_text,
        translated="住手！",
        severity="high",
        source_language="ko",
        source_text=primary_text,
        primary_evidence=primary_text,
        secondary_evidence=secondary_text,
        review_required=True,
    )

    risks = _merge_arbitration_risks(
        [regular_risk], candidates, translated, DATA_DIR, "ko",
    )

    # Find the regular risk item (not an arbitration-specific one)
    regular = [r for r in risks if not r.key.startswith("arbitration:")]
    assert len(regular) == 1

    # Verify source_text matches the canonical from arbitration
    units = arbitrate_candidates(
        candidates, source_language="ko", data_dir=DATA_DIR,
    )
    unit = units.get("1")
    assert unit is not None
    assert unit.canonical_source_text
    assert regular[0].source_text == unit.canonical_source_text


def test_merge_arbitration_risks_preserves_source_text_when_identical():
    """When primary == secondary (identical), source_text stays the same."""
    from pipeline.risk_queue import RiskItem

    shared_text = "보이드 스톰이 왔다."
    candidates = [_candidate("1", shared_text, shared_text)]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "虚质磁暴来了。")]

    regular_risk = RiskItem(
        key="1", subtitle_id=1,
        start="00:00:00,000", end="00:00:02,000",
        score=5, reasons=["term_mismatch"],
        english=shared_text,
        capcut_en=shared_text,
        whisper_en=shared_text,
        translated="虚质磁暴来了。",
        severity="medium",
        source_language="ko",
        source_text=shared_text,
        primary_evidence=shared_text,
        secondary_evidence=shared_text,
        review_required=False,
    )

    risks = _merge_arbitration_risks(
        [regular_risk], candidates, translated, DATA_DIR, "ko",
    )

    regular = [r for r in risks if not r.key.startswith("arbitration:")]
    assert len(regular) == 1
    assert regular[0].source_text == shared_text


def test_merge_arbitration_risks_does_not_touch_english():
    """en source_language never enters arbitration; source_text unchanged."""
    from pipeline.risk_queue import RiskItem

    candidates = [_candidate("1", "Hello there", "Hello there", lang="en")]
    translated = [_subtitle(1, "00:00:00,000", "00:00:02,000", "你好")]

    original_source = "Hello there"
    regular_risk = RiskItem(
        key="1", subtitle_id=1,
        start="00:00:00,000", end="00:00:02,000",
        score=3, reasons=["text_conflict"],
        english=original_source,
        capcut_en=original_source,
        whisper_en=original_source,
        translated="你好",
        severity="advisory",
        source_language="en",
        source_text=original_source,
        primary_evidence=original_source,
        secondary_evidence=original_source,
        review_required=False,
    )

    risks = _merge_arbitration_risks(
        [regular_risk], candidates, translated, DATA_DIR, "en",
    )

    assert risks == [regular_risk]
    assert risks[0].source_text == original_source


# ---------- union 文本覆盖 ----------

def test_write_union_ko_uses_canonical(tmp_path):
    """ko union：canonical（ASR 归一化后）覆盖主文本，ゲイード→에이메스。"""
    primary = tmp_path / "p.ko.srt"
    secondary = tmp_path / "s.ko.srt"
    union = tmp_path / "u.ko.srt"
    primary.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n진짜 내가 갈게 게이드\n",
        encoding="utf-8",
    )
    secondary.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n에이메스 진짜 내가 갈게\n",
        encoding="utf-8",
    )
    candidates, _, _ = _write_union(
        str(primary), str(secondary), str(union),
        source_language="ko",
    )
    text = union.read_text(encoding="utf-8")
    assert "에이메스" in text
    # 仲裁 JSON 旁路产物存在
    assert os.path.exists(str(union) + ".arbitration.json")


def test_write_union_en_untouched(tmp_path):
    """en union：不启用仲裁，文本保持原主源。"""
    primary = tmp_path / "p.en.srt"
    secondary = tmp_path / "s.en.srt"
    union = tmp_path / "u.en.srt"
    primary.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello there\n",
        encoding="utf-8",
    )
    secondary.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello there friend\n",
        encoding="utf-8",
    )
    candidates, _, _ = _write_union(
        str(primary), str(secondary), str(union),
        source_language="en",
    )
    text = union.read_text(encoding="utf-8")
    assert "Hello there" in text
    assert not os.path.exists(str(union) + ".arbitration.json")


def test_accepted_canonical_after_is_the_round1_source_text(tmp_path):
    """生产写回 helper 保证 trace canonical_after 与 Round1 读取 union 一致。"""
    import json
    from pipeline.parser.srt_parser import parse_srt
    from pipeline.preprocess.source_arbitration import _normalise_for_compare
    from pipeline.preprocess.source_reconstruction import (
        reconstruct_sources, update_trace_applied,
    )

    union = tmp_path / "round1-input.ko.srt"
    union.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n에이메스가 왔어\n",
        encoding="utf-8",
    )
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="ko",
        primary_evidence="에이메스가 왔어",
        secondary_evidence="에이메스가 직접 왔어",
        canonical_source_text="에이메스가 왔어",
        source_decision="unresolved", source_confidence=0.3,
        source_conflict=True,
    )

    class Translator:
        def _request_api(self, prompt, **kwargs):
            return "에이메스가 직접 왔어", 1, 1

    trace_path = str(tmp_path / "round1-input.ko.reconstruction.json")
    rebuilt = reconstruct_sources(
        {unit.key: unit}, [], source_language="ko", data_dir=DATA_DIR,
        translator=Translator(), trace_path=trace_path,
    )
    assert rebuilt[unit.key]["acceptance"] == "accepted"

    replaced, applied_keys = _apply_accepted_reconstructions(
        str(union), {unit.key: unit}, rebuilt,
    )
    update_trace_applied(trace_path, applied_keys)

    assert replaced == 1
    round1_source_text = parse_srt(str(union))[0].text
    trace = json.loads(open(trace_path, encoding="utf-8").read())
    entry = trace["entries"][0]
    assert entry["applied"] is True
    assert _normalise_for_compare(
        entry["canonical_after"], "ko",
    ) == _normalise_for_compare(round1_source_text, "ko")


def test_rejected_reconstruction_is_not_written_to_round1_union(tmp_path):
    union = tmp_path / "round1-input.ko.srt"
    union.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n에이메스가 왔어\n",
        encoding="utf-8",
    )
    unit = ArbitratedUnit(
        key="1", start_ms=0, end_ms=2000, source_language="ko",
        canonical_source_text="에이메스가 왔어",
    )
    rebuilt = {
        "1": {
            "text": "메카우터가 모두를 구했어",
            "decision": "reconstructed",
            "acceptance": "rejected",
        },
    }

    replaced, applied_keys = _apply_accepted_reconstructions(
        str(union), {unit.key: unit}, rebuilt,
    )

    assert replaced == 0
    assert applied_keys == set()
    assert "에이메스가 왔어" in union.read_text(encoding="utf-8")


# ---------- M-04: display timing 选源（2026-08-11 Phase 7） ----------

def test_display_map_uses_secondary_timing_when_secondary_wins(tmp_path):
    """M-04：KO 双源仲裁选中 secondary 为 canonical 时，
    display-map 时间窗/span 必须来自 secondary（Whisper 时间），
    不再硬选 primary。

    场景：primary 含 unknown 实体（퍼블）+ 时间漂移（4-8s），
    secondary 是 glossary 已知实体（에이메스）+ 精确时间（5-7s），
    时间部分重叠 → 匹配成一条 → 仲裁选 secondary → display-map
    时间必须用 secondary 的 5-7s，而不是 primary 的 4-8s。
    """
    primary = tmp_path / "p.ko.srt"
    secondary = tmp_path / "s.ko.srt"
    union = tmp_path / "u.ko.srt"
    display_map = tmp_path / "u.ko.display-map.json"
    primary.write_text(
        "1\n00:00:04,000 --> 00:00:08,000\n진짜 내가 갈게 퍼블\n",
        encoding="utf-8",
    )
    secondary.write_text(
        "1\n00:00:05,000 --> 00:00:07,000\n에이메스 진짜 내가 갈게\n",
        encoding="utf-8",
    )
    _write_union(
        str(primary), str(secondary), str(union),
        source_language="ko", display_map_path=str(display_map),
    )
    import json as _json
    mapping = _json.loads(display_map.read_text(encoding="utf-8"))
    starts = [span["start"] for spans in mapping.values() for span in spans]
    # 时间来自 secondary（5s），而不是漂移的 primary（4s）
    assert "00:00:05,000" in starts, starts
    assert "00:00:04,000" not in starts, starts


def test_display_map_primary_timing_when_primary_wins(tmp_path):
    """M-04 反例：primary 被仲裁选中（identical/primary）时，
    display-map 保持 primary 时间（不回归）。"""
    primary = tmp_path / "p2.ko.srt"
    secondary = tmp_path / "s2.ko.srt"
    union = tmp_path / "u2.ko.srt"
    display_map = tmp_path / "u2.ko.display-map.json"
    primary.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\n에이메스 진짜 내가 갈게\n",
        encoding="utf-8",
    )
    secondary.write_text(
        "1\n00:00:01,000 --> 00:00:03,500\n에이메스 진짜 내가 갈게\n",
        encoding="utf-8",
    )
    _write_union(
        str(primary), str(secondary), str(union),
        source_language="ko", display_map_path=str(display_map),
    )
    import json as _json
    mapping = _json.loads(display_map.read_text(encoding="utf-8"))
    starts = [span["start"] for spans in mapping.values() for span in spans]
    assert "00:00:02,000" in starts, starts
