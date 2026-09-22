"""阶段 D 测试：Source Reconstruction（mock LLM 验证决策逻辑与 prompt 构建）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.candidates import SourceCandidate
from pipeline.preprocess.source_arbitration import (
    ArbitratedUnit, arbitrate_candidates,
)
from pipeline.preprocess.source_reconstruction import (
    build_reconstruction_prompt, _parse_reconstruction, reconstruct_sources,
    should_reconstruct, update_trace_applied,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class FakeTranslator:
    """mock LLM：按输入 prompt 是否含 '게이드' 返回重建结果。"""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def _request_api(self, prompt: str, **kwargs):
        self.calls += 1
        return self.response, 100, 20


def _unit(key="1", primary="진짜 내가 갈게 게이드",
          secondary="에이메스 진짜 내가 갈게", decision="unresolved",
          conflict=True, confidence=0.3, unknown=None):
    return ArbitratedUnit(
        key=key, start_ms=0, end_ms=2000, source_language="ko",
        primary_evidence=primary, secondary_evidence=secondary,
        canonical_source_text=primary,
        source_decision=decision, source_confidence=confidence,
        source_conflict=conflict,
        unknown_entities=unknown or [],
    )


def test_should_reconstruct_triggers():
    """unresolved / 低置信冲突 / 未知专名冲突 → 触发重建。"""
    assert should_reconstruct(_unit(decision="unresolved", confidence=0.3))
    assert should_reconstruct(_unit(decision="primary", confidence=0.3, conflict=True))
    assert should_reconstruct(_unit(decision="primary", confidence=0.5,
                                    conflict=True, unknown=["메카우터"]))
    assert not should_reconstruct(_unit(decision="primary", confidence=0.6,
                                        conflict=False))
    assert not should_reconstruct(_unit(decision="identical", confidence=0.95,
                                        conflict=False))


def test_build_prompt_contains_evidence_and_rules():
    """prompt 必须包含双证据、术语、硬规则、不翻译指令。"""
    from pipeline.preprocess.source_arbitration import ArbitrationKnowledge
    knowledge = ArbitrationKnowledge.from_data_dir(DATA_DIR, game="wuwa")
    unit = _unit()
    prompt = build_reconstruction_prompt(unit, ["아 전에 봤었지"], ["그래서 말이지"], knowledge)
    assert "게이드" in prompt and "에이메스" in prompt
    assert "不是翻译" in prompt or "恢复" in prompt
    assert "unresolved" in prompt
    assert "아 전에 봤었지" in prompt and "그래서 말이지" in prompt


def test_parse_reconstruction():
    """解析：韩文句通过；unresolved/拒绝/空 → 空。"""
    assert _parse_reconstruction("에이메스, 진짜 내가 갈게") == "에이메스, 진짜 내가 갈게"
    assert _parse_reconstruction("unresolved") == ""
    assert _parse_reconstruction("无法可靠判断，返回 unresolved") == ""
    assert _parse_reconstruction("") == ""
    # 纯解释（无韩文）→ 空
    assert _parse_reconstruction("这是主播在说游戏内容。") == ""


def test_reconstruct_sources_uses_canonical_rebuild():
    """mock 重建：unresolved 句被重建为韩文，结果被采纳。"""
    candidate = SourceCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="이번 작동에서 메카우터는 오려",
        secondary_evidence="카세차한다 뭐야",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate], source_language="ko", data_dir=DATA_DIR,
    )
    assert units["1"].source_decision == "unresolved"
    translator = FakeTranslator("카세차한다 뭐야")
    results = reconstruct_sources(
        units, [candidate], source_language="ko",
        data_dir=DATA_DIR, translator=translator,
        max_units=5,
    )
    assert translator.calls == 1
    assert results["1"]["decision"] == "reconstructed"
    assert results["1"]["acceptance"] == "accepted"
    assert results["1"]["text"] == "카세차한다 뭐야"


def test_reconstruct_sources_unresolved_when_llm_refuses():
    """LLM 无法判断 → decision=unresolved，不编造。"""
    candidate = SourceCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="아무튼 온 나이 너희가 무너질 거야",
        secondary_evidence="카세차한다 뭐야",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate], source_language="ko", data_dir=DATA_DIR,
    )
    translator = FakeTranslator("unresolved")
    results = reconstruct_sources(
        units, [candidate], source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=5,
    )
    assert results["1"]["decision"] == "unresolved"
    assert results["1"]["text"] == ""


@pytest.mark.parametrize(
    ("primary", "secondary", "rebuilt", "reason"),
    [
        ("에이메스가 3시에 왔어", "에이메스가 3시에 왔어", "에이메스가 4시에 왔어", "number_changed"),
        ("에이메스가 왔어", "에이메스가 왔어", "에이메스가 안 왔어", "negation_changed"),
        ("오늘 직접 문을 열었어 정말", "정말 오늘 문을 직접 열었어", "메카우터가 직접 문을 열었어 정말", "new_entity"),
        ("에이메스가 왔어", "에이메스가 왔어", "에이메스가 보이드 스톰을 막고 모두를 구했어", "large_unsupported_addition"),
        ("에이메스가 왔어", "에이메스가 왔어", "완전히 다른 이야기입니다", "low_evidence_similarity"),
        ("에이메스가 왔어", "에이메스가 왔어", "这是爱弥斯来了", "target_language_content"),
    ],
    ids=["number", "negation", "entity", "addition", "low_similarity", "chinese"],
)
def test_reconstruction_acceptance_gate_rejects_unsafe_rewrites(
    primary, secondary, rebuilt, reason,
):
    unit = _unit(primary=primary, secondary=secondary, decision="unresolved")
    result = reconstruct_sources(
        {unit.key: unit}, [], source_language="ko", data_dir=DATA_DIR,
        translator=FakeTranslator(rebuilt), max_units=5,
    )[unit.key]

    assert result["acceptance"] == "rejected"
    assert reason in result["acceptance_reasons"]


@pytest.mark.parametrize(
    ("primary", "secondary", "canonical", "rebuilt", "reason"),
    [
        ("게이드가 왔어", "전혀 다른 말이야", "게이드가 왔어", "에이메스가 왔어", "asr_correction"),
        ("에이메즈가 왔어", "전혀 다른 말이야", "에이메즈가 왔어", "에이메스가 왔어", "glossary_entity_repair"),
        ("에이메스가", "에이메스가 직접 왔어", "에이메스가", "에이메스가 직접 왔어", "full_source_replaces_fragment"),
        ("에이메스가 직접", "직접 문을 열었어", "에이메스가 직접", "에이메스가 직접 문을 열었어", "dual_source_supported_completion"),
    ],
    ids=["asr", "glossary_entity", "fragment", "dual_source_completion"],
)
def test_reconstruction_acceptance_gate_accepts_evidence_backed_repairs(
    primary, secondary, canonical, rebuilt, reason,
):
    unit = _unit(primary=primary, secondary=secondary, decision="unresolved")
    unit.canonical_source_text = canonical
    result = reconstruct_sources(
        {unit.key: unit}, [], source_language="ko", data_dir=DATA_DIR,
        translator=FakeTranslator(rebuilt), max_units=5,
    )[unit.key]

    assert result["acceptance"] == "accepted"
    assert reason in result["acceptance_reasons"]
    assert result["text"] == rebuilt


def test_reconstruction_acceptance_gate_leaves_ambiguous_rewrite_unresolved():
    unit = _unit(
        primary="에이메스가 아마 올 거야",
        secondary="에이메스가 곧 올 거야",
        decision="unresolved",
    )
    result = reconstruct_sources(
        {unit.key: unit}, [], source_language="ko", data_dir=DATA_DIR,
        translator=FakeTranslator("에이메스가 올 것 같아"), max_units=5,
    )[unit.key]

    assert result["acceptance"] == "unresolved"
    assert result["text"] == ""


def test_reconstruct_sources_skips_clean_and_en():
    """clean 单元与 en 不触发重建。"""
    candidate = SourceCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="진짜 게이트를 열었네",
        secondary_evidence="진짜 게이트를 열었네",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate], source_language="ko", data_dir=DATA_DIR,
    )
    translator = FakeTranslator("진짜 게이트를 열었네")
    results = reconstruct_sources(
        units, [candidate], source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=5,
    )
    assert translator.calls == 0
    assert results == {}

    # en
    cand_en = SourceCandidate(
        key="1", start="00:00:00,000", end="00:00:02,000",
        primary_evidence="Hello there", secondary_evidence="",
        primary_source="capcut", secondary_source="",
        source_language="en", primary_coverage=1.0, secondary_coverage=0.0,
        flags=[],
    )
    results_en = reconstruct_sources(
        {"1": _unit(decision="unresolved")}, [cand_en],
        source_language="en", data_dir=DATA_DIR, translator=translator,
    )
    assert results_en == {}


def test_reconstruct_max_units_cap():
    """max_units 上限：只调用前 N 个高风险句。"""
    candidates = []
    units = {}
    translator = FakeTranslator("에이메스 진짜 내가 갈게")
    for i in range(5):
        key = str(i)
        candidates.append(SourceCandidate(
            key=key, start=f"00:00:{i:02d},000", end=f"00:00:{i:02d},500",
            primary_evidence=f"진짜 내가 갈게 게이드{i}",
            secondary_evidence="에이메스 진짜 내가 갈게",
            primary_source="youtube", secondary_source="whisper",
            source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
            flags=[],
        ))
        units[key] = _unit(key=key, decision="unresolved", confidence=0.2,
                           primary=f"진짜 내가 갈게 게이드{i}")
    results = reconstruct_sources(
        units, candidates, source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=3,
    )
    assert translator.calls == 3
    assert len(results) == 3


# ------------------------------------------------------------------
# P0-1 新增测试：trace 落盘、多语言、集成路径
# ------------------------------------------------------------------


def test_parse_reconstruction_japanese():
    """日语重建结果：含假名的日文句通过；无假名 → 空。"""
    assert _parse_reconstruction("エイメス、マジでやるよ", "ja") == "エイメス、マジでやるよ"
    assert _parse_reconstruction("unresolved", "ja") == ""
    assert _parse_reconstruction("这是主播在说游戏内容。", "ja") == ""
    # 韩语参数下日语假名也应通过（_SOURCE_SCRIPT_RE 覆盖两种文字）
    assert _parse_reconstruction("エイメス", "ko") == "エイメス"


def test_build_prompt_japanese_uses_correct_label():
    """日语 prompt 必须说'日语'不是'韩语'。"""
    from pipeline.preprocess.source_arbitration import ArbitrationKnowledge
    knowledge = ArbitrationKnowledge.from_data_dir(
        DATA_DIR, game="wuwa", source_language="ja",
    )
    unit = ArbitratedUnit(
        key="ja1", start_ms=0, end_ms=2000, source_language="ja",
        primary_evidence="エイメスが来た", secondary_evidence="エイメス来た",
        canonical_source_text="エイメスが来た",
        source_decision="unresolved", source_confidence=0.3,
        source_conflict=True, unknown_entities=[],
    )
    prompt = build_reconstruction_prompt(unit, ["前の文"], ["後の文"], knowledge)
    assert "日语" in prompt
    assert "韩语" not in prompt
    assert "不是翻译" in prompt
    assert "unresolved" in prompt


def test_reconstruct_trace_saved(tmp_path):
    """重建后 trace 文件必须落盘，包含 5 种状态区分。"""
    import json
    candidate = SourceCandidate(
        key="00:00:00,000|00:00:02,000|1",
        start="00:00:00,000", end="00:00:02,000",
        primary_evidence="이번 작동에서 메카우터는 오려",
        secondary_evidence="카세차한다 뭐야",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    # 再加一个 clean 单元（not_eligible）
    clean_cand = SourceCandidate(
        key="00:00:02,000|00:00:04,000|2",
        start="00:00:02,000", end="00:00:04,000",
        primary_evidence="진짜 게이트를 열었네",
        secondary_evidence="진짜 게이트를 열었네",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate, clean_cand], source_language="ko", data_dir=DATA_DIR,
    )
    translator = FakeTranslator("카세차한다 뭐야")
    trace_path = str(tmp_path / "test.reconstruction.json")
    results = reconstruct_sources(
        units, [candidate, clean_cand], source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=5,
        trace_path=trace_path,
    )
    # trace 文件存在
    assert os.path.isfile(trace_path)
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    summary = payload["summary"]
    # 至少 1 个 eligible + called + resolved
    assert summary["eligible"] >= 1
    assert summary["called"] >= 1
    assert summary["resolved"] >= 1
    assert summary["accepted"] >= 1
    assert summary["rejected"] == 0
    assert summary["accept"] == summary["accepted"]
    assert summary["reject"] == summary["rejected"]
    # 至少 1 个 not_eligible（clean 单元）
    entries = payload["entries"]
    statuses = {e["reconstruction_status"] for e in entries}
    assert "called_resolved" in statuses
    assert "not_eligible" in statuses
    # resolved 条目有 reconstructed_text
    resolved = [e for e in entries if e["reconstruction_status"] == "called_resolved"]
    assert resolved[0]["reconstructed_text"] == "카세차한다 뭐야"
    assert resolved[0]["acceptance"] == "accepted"
    # applied 初始为 False（写回前）
    assert resolved[0]["applied"] is False


def test_reconstruct_trace_error_status(tmp_path):
    """LLM 调用异常 → status=called_error，不编造。"""
    import json
    candidate = SourceCandidate(
        key="00:00:00,000|00:00:02,000|1",
        start="00:00:00,000", end="00:00:02,000",
        primary_evidence="아무튼 온 나이 너희가 무너질 거야",
        secondary_evidence="카세차한다 뭐야",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate], source_language="ko", data_dir=DATA_DIR,
    )

    class ErrorTranslator:
        def _request_api(self, prompt, **kwargs):
            raise ConnectionError("API down")

    trace_path = str(tmp_path / "error.reconstruction.json")
    results = reconstruct_sources(
        units, [candidate], source_language="ko",
        data_dir=DATA_DIR, translator=ErrorTranslator(), max_units=5,
        trace_path=trace_path,
    )
    assert results[candidate.key]["decision"] == "error"
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    entries = payload["entries"]
    error_entry = [e for e in entries if e["reconstruction_status"] == "called_error"]
    assert len(error_entry) == 1
    assert error_entry[0]["reconstruction_error"] == "ConnectionError"


def test_reconstruct_trace_eligible_not_called(tmp_path):
    """超过 cap 的 eligible 单元记为 eligible_not_called。"""
    import json
    # 使用完全不同的短句，确保 similarity < 0.5 → unresolved
    pairs = [
        ("아무거나 한다", "뭔가 다르다"),
        ("이게 뭐야", "저건 뭐지"),
        ("어디 갔어", "언제 왔어"),
        ("왜 그래", "어떡해"),
        ("빨리 와", "천천히 가"),
    ]
    candidates = []
    for i, (primary_text, secondary_text) in enumerate(pairs):
        key = f"00:00:{i:02d},000|00:00:{i:02d},500|{i+1}"
        candidates.append(SourceCandidate(
            key=key, start=f"00:00:{i:02d},000", end=f"00:00:{i:02d},500",
            primary_evidence=primary_text,
            secondary_evidence=secondary_text,
            primary_source="youtube", secondary_source="whisper",
            source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
            flags=[],
        ))
    units = arbitrate_candidates(
        candidates, source_language="ko", data_dir=DATA_DIR,
    )
    # 确保至少有 3 个 unresolved（触发 should_reconstruct）
    eligible_count = sum(1 for u in units.values() if should_reconstruct(u))
    assert eligible_count >= 3, f"Expected >= 3 eligible, got {eligible_count}"
    translator = FakeTranslator("에이메스 진짜 간다")
    trace_path = str(tmp_path / "cap.reconstruction.json")
    results = reconstruct_sources(
        units, candidates, source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=2,
        trace_path=trace_path,
    )
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    statuses = {e["reconstruction_status"] for e in payload["entries"]}
    assert "eligible_not_called" in statuses
    assert payload["summary"]["called"] == 2


def test_update_trace_applied_after_writeback(tmp_path):
    """重建结果写回 union 后，update_trace_applied 更新 applied=True。"""
    import json
    candidate = SourceCandidate(
        key="00:00:00,000|00:00:02,000|1",
        start="00:00:00,000", end="00:00:02,000",
        primary_evidence="이번 작동에서 메카우터는 오려",
        secondary_evidence="카세차한다 뭐야",
        primary_source="youtube", secondary_source="whisper",
        source_language="ko", primary_coverage=1.0, secondary_coverage=1.0,
        flags=[],
    )
    units = arbitrate_candidates(
        [candidate], source_language="ko", data_dir=DATA_DIR,
    )
    translator = FakeTranslator("카세차한다 뭐야")
    trace_path = str(tmp_path / "applied.reconstruction.json")
    results = reconstruct_sources(
        units, [candidate], source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=5,
        trace_path=trace_path,
    )
    # 写回前 applied=False
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    resolved = [e for e in payload["entries"] if e["reconstruction_status"] == "called_resolved"]
    assert resolved[0]["applied"] is False
    # 模拟写回
    update_trace_applied(trace_path, {candidate.key})
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    resolved = [e for e in payload["entries"] if e["reconstruction_status"] == "called_resolved"]
    assert resolved[0]["applied"] is True
    assert payload["summary"]["applied"] == 1


def test_reconstruction_full_pipeline_path(tmp_path):
    """集成测试：双源冲突 → 仲裁低置信 → 重建 eligible → mocked LLM resolved →
    canonical 被替换 → union SRT 含新文本（Round 1 读到的就是新文本）。

    这不是只测函数——走真实 _write_union + arbitrate_candidates + reconstruct_sources
    + union 写回路径，证明 Round 1（从 union SRT 读取）会拿到重建后的文本。
    """
    import json
    from pipeline.parser.srt_parser import parse_srt, save_srt, timestamp_to_ms
    from pipeline.long_video import _write_union, _stage_paths

    # 1) 创建冲突的双源 SRT
    primary_srt = tmp_path / "primary.ko.srt"
    secondary_srt = tmp_path / "secondary.ko.srt"
    primary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n이번 작동에서 메카우터는 오려\n",
        encoding="utf-8",
    )
    secondary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n카세차한다 뭐야\n",
        encoding="utf-8",
    )
    output = str(tmp_path / "test.zh.srt")
    paths = _stage_paths(output, "ko")

    # 2) _write_union：仲裁 → canonical 覆盖 union
    candidates, _, _ = _write_union(
        str(primary_srt), str(secondary_srt), paths["union"],
        source_language="ko",
    )
    assert len(candidates) >= 1

    # 3) 再次仲裁（与 long_video.py 真实流程一致）
    from pipeline.preprocess.source_arbitration import arbitrate_candidates
    from pipeline.preprocess.source_reconstruction import (
        reconstruct_sources, should_reconstruct, update_trace_applied,
    )
    units = arbitrate_candidates(
        candidates, source_language="ko", data_dir=DATA_DIR,
    )
    high_risk = [u for u in units.values() if should_reconstruct(u)]
    assert len(high_risk) >= 1, "至少 1 个高风险单元应触发重建"

    # 4) 重建（mock LLM 返回 resolved 韩文句）
    translator = FakeTranslator("카세차한다 뭐야")
    rebuilt = reconstruct_sources(
        units, candidates, source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=25,
        trace_path=paths["reconstruction_trace"],
    )
    # 至少 1 个 resolved
    resolved_keys = [
        k for k, v in rebuilt.items() if v["decision"] == "reconstructed"
    ]
    assert len(resolved_keys) >= 1, "至少 1 个重建成功"

    # 5) 写回 union（与 long_video.py 真实流程一致）
    union_subs = parse_srt(paths["union"])
    by_time = {
        (u.start_ms, u.end_ms): u for u in units.values()
    }
    replaced = 0
    applied_keys: set[str] = set()
    for sub in union_subs:
        unit = by_time.get((
            timestamp_to_ms(sub.start), timestamp_to_ms(sub.end),
        ))
        entry = rebuilt.get(unit.key) if unit else None
        if entry and entry.get("text"):
            old_text = sub.text
            sub.text = entry["text"]
            replaced += 1
            applied_keys.add(unit.key)
    save_srt(paths["union"], union_subs)
    update_trace_applied(paths["reconstruction_trace"], applied_keys)

    assert replaced >= 1, "至少 1 行 union 被替换"

    # 6) 验证：重新解析 union（Round 1 读取的文件），确认含新文本
    final_union = parse_srt(paths["union"])
    new_texts = [sub.text for sub in final_union]
    assert "카세차한다 뭐야" in new_texts, \
        "union SRT 必须含 accepted 的 canonical_after（Round 1 会读到）"

    # 7) trace 文件验证
    trace = json.loads(
        open(paths["reconstruction_trace"], encoding="utf-8").read()
    )
    assert trace["summary"]["applied"] >= 1
    assert trace["summary"]["resolved"] >= 1


def test_reconstruction_unresolved_keeps_canonical(tmp_path):
    """集成测试 2：重建返回 unresolved → 不编造、保留原 canonical、trace 记录。

    走真实 _write_union + arbitrate + reconstruct 路径。
    """
    import json
    from pipeline.parser.srt_parser import parse_srt, save_srt, timestamp_to_ms
    from pipeline.long_video import _write_union, _stage_paths

    primary_srt = tmp_path / "primary.ko.srt"
    secondary_srt = tmp_path / "secondary.ko.srt"
    primary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n이번 작동에서 메카우터는 오려\n",
        encoding="utf-8",
    )
    secondary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n카세차한다 뭐야\n",
        encoding="utf-8",
    )
    output = str(tmp_path / "test.zh.srt")
    paths = _stage_paths(output, "ko")

    candidates, _, _ = _write_union(
        str(primary_srt), str(secondary_srt), paths["union"],
        source_language="ko",
    )

    from pipeline.preprocess.source_arbitration import arbitrate_candidates
    from pipeline.preprocess.source_reconstruction import (
        reconstruct_sources, should_reconstruct,
    )
    units = arbitrate_candidates(
        candidates, source_language="ko", data_dir=DATA_DIR,
    )

    # mock LLM 全部返回 unresolved
    translator = FakeTranslator("unresolved")
    rebuilt = reconstruct_sources(
        units, candidates, source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=25,
        trace_path=paths["reconstruction_trace"],
    )

    # 所有结果都是 unresolved，没有 text
    for key, entry in rebuilt.items():
        assert entry["decision"] == "unresolved"
        assert entry["text"] == ""

    # union 不被修改（重建没有 text 可写回）
    union_subs = parse_srt(paths["union"])
    by_time = {
        (u.start_ms, u.end_ms): u for u in units.values()
    }
    replaced = 0
    for sub in union_subs:
        unit = by_time.get((
            timestamp_to_ms(sub.start), timestamp_to_ms(sub.end),
        ))
        entry = rebuilt.get(unit.key) if unit else None
        if entry and entry.get("text"):
            replaced += 1
    assert replaced == 0, "unresolved 时不应替换任何 union 行"

    # trace 记录了 called_unresolved
    trace = json.loads(
        open(paths["reconstruction_trace"], encoding="utf-8").read()
    )
    assert trace["summary"]["unresolved"] >= 1
    assert trace["summary"]["resolved"] == 0
    assert trace["summary"]["applied"] == 0


def test_reconstruction_result_is_source_language(tmp_path):
    """集成测试 3：重建结果必须仍是韩语（禁止变成二次翻译）。

    mock LLM 如果返回中文 → _parse_reconstruction 应拒绝（无韩文音节）。
    """
    from pipeline.parser.srt_parser import parse_srt, timestamp_to_ms
    from pipeline.long_video import _write_union, _stage_paths

    primary_srt = tmp_path / "primary.ko.srt"
    secondary_srt = tmp_path / "secondary.ko.srt"
    primary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n이번 작동에서 메카우터는 오려\n",
        encoding="utf-8",
    )
    secondary_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n카세차한다 뭐야\n",
        encoding="utf-8",
    )
    output = str(tmp_path / "test.zh.srt")
    paths = _stage_paths(output, "ko")

    candidates, _, _ = _write_union(
        str(primary_srt), str(secondary_srt), paths["union"],
        source_language="ko",
    )

    from pipeline.preprocess.source_arbitration import arbitrate_candidates
    from pipeline.preprocess.source_reconstruction import (
        reconstruct_sources, should_reconstruct,
    )
    units = arbitrate_candidates(
        candidates, source_language="ko", data_dir=DATA_DIR,
    )

    # mock LLM 返回中文翻译（不是韩语原句）
    translator = FakeTranslator("这是爱弥斯要出场了")
    rebuilt = reconstruct_sources(
        units, candidates, source_language="ko",
        data_dir=DATA_DIR, translator=translator, max_units=25,
        trace_path=paths["reconstruction_trace"],
    )

    # 中文输出被 acceptance gate 明确拒绝
    for key, entry in rebuilt.items():
        assert entry["decision"] == "rejected"
        assert entry["acceptance"] == "rejected"
        assert "target_language_content" in entry["acceptance_reasons"]
        assert entry["text"] == ""

    import json
    trace = json.loads(
        open(paths["reconstruction_trace"], encoding="utf-8").read()
    )
    assert trace["summary"]["rejected"] >= 1
    rejected = [
        entry for entry in trace["entries"]
        if entry["acceptance"] == "rejected"
    ]
    assert rejected[0]["canonical_after"] == rejected[0]["canonical_before"]
    assert rejected[0]["applied"] is False
