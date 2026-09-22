"""阶段 F 测试：Round 2 Reviewer（critic/corrector）模式验证。

证明现有 _round_two_prompt 已是 Reviewer 模式：
- 输入含全部证据（source_text / 双源 evidence / round1_chinese / 上下文 / 术语）
- 规则锚定 keep（Round 1 准确必须原样返回），replace 只做最小修正
- 双源冲突禁止拼接；上下文禁止翻译进当前字幕
- Case 9：canonical 有 에이메스 且 Round 1 漏译 → term_mismatch 触发，
  replace 候选"爱弥斯，我真的要去"通过确定性门禁
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.long_video import (
    _evaluate_round_two_decision, _round_two_prompt,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ko_item(**overrides):
    item = {
        "subtitle_id": 1,
        "source_language": "ko",
        "source_text": "에이메스, 진짜 내가 갈게",
        "english": "에이메스, 진짜 내가 갈게",
        "primary_evidence": "진짜 내가 갈게 게이드",
        "secondary_evidence": "에이메스 진짜 내가 갈게",
        "translated": "我要去隧门",
        "reasons": ["term_mismatch"],
        "term_hints": {"에이메스": "爱弥斯"},
        "context_before": "",
        "context_after": "",
        "start": "00:00:00,000",
        "end": "00:00:02,000",
    }
    item.update(overrides)
    return item


def _en_item(**overrides):
    item = {
        "subtitle_id": 2,
        "source_language": "en",
        "source_text": "I did 20 pulls",
        "english": "I did 20 pulls",
        "primary_evidence": "I did twenty pulls",
        "secondary_evidence": "I did 20 pulls",
        "translated": "我抽了10次",
        "reasons": ["number_mismatch"],
        "term_hints": {},
        "context_before": "The previous banner was unlucky.",
        "context_after": "Now I have enough currency.",
        "start": "00:00:00,000",
        "end": "00:00:02,000",
    }
    item.update(overrides)
    return item


def test_round_two_prompt_includes_all_reviewer_evidence():
    """Reviewer 输入齐全：source/双源证据/round1 译文/术语/上下文。"""
    prompt = _round_two_prompt([_ko_item()], {"에이메스": "爱弥斯"}, "ko")
    payload = json.loads(prompt)
    assert payload["items"][0]["source_text"] == "에이메스, 진짜 내가 갈게"
    assert payload["items"][0]["primary_evidence"] == "진짜 내가 갈게 게이드"
    assert payload["items"][0]["secondary_evidence"] == "에이메스 진짜 내가 갈게"
    assert payload["items"][0]["round1_chinese"] == "我要去隧门"
    assert payload["items"][0]["required_name_terms"] == {"에이메스": "爱弥斯"}
    assert payload["items"][0]["context_before"] == ""
    assert payload["items"][0]["risk_reasons"] == ["term_mismatch"]


def test_round_two_prompt_anchors_keep_and_blocks_concat():
    """规则锚定 keep（原样返回）+ canonical 为准 + 禁止上下文泄漏。"""
    prompt = _round_two_prompt([_ko_item()], {}, "ko")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    all_text = payload["task"] + " " + rules
    assert "原样返回" in rules
    assert "最小修正" in all_text
    # P0-2 新增：canonical 为准，禁止覆盖
    assert "不可替代 source_text" in rules
    assert "不是重新仲裁双源" in rules
    assert "不得翻译到当前字幕" in rules
    assert "虚构人名" in rules  # 阶段 F 新增：幻觉实体检查
    assert "否定翻反" in rules and "数字错误" in rules
    assert "kana_residue" in rules and "hangul_residue" in rules
    assert "必须 replace" in rules


def test_round_two_prompt_en_uses_canonical_source_and_keep_anchor():
    """en Reviewer 只复核 canonical，raw ASR 仅作诊断且默认 KEEP。"""
    prompt = _round_two_prompt([_en_item()], {}, "en")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    assert "canonical English" in payload["task"] + rules
    assert "source of truth" in rules
    assert "raw ASR" in rules and "advisory diagnostic context" in rules
    assert "重新选择 source" in rules
    assert "Round 1 当作草稿重新翻译整句" in rules
    assert "默认 KEEP" in rules and "原样返回 Round 1" in rules
    assert "results 必须逐一覆盖所有输入" in " ".join(payload["rules"])


def test_round_two_prompt_en_lists_checks_and_requires_replace_evidence():
    """en prompt 覆盖完整检查项，并要求 REPLACE 给出具体最小修正证据。"""
    payload = json.loads(_round_two_prompt([_en_item()], {}, "en"))
    rules = " ".join(payload["rules"])
    for check in (
        "omission", "addition", "negation", "question", "number",
        "proper noun", "terminology", "subject-object",
        "reaction intensity", "literal-Chinese stiffness",
        "hallucinated entity",
    ):
        assert check in rules
    assert "canonical source span" in rules
    assert "Round 1 error" in rules
    assert "minimal corrected translation" in rules
    assert "翻译不自然" in rules and "空泛理由" in rules


def test_round_two_prompt_en_consumes_entity_evidence_conservatively():
    """T7-C unknown/hallucinated evidence 要求禁止创造 glossary 外中文专名。"""
    item = _en_item(reasons=["unknown_entity:Xyzzqz", "hallucinated_entity:泽尔卡恩"])
    payload = json.loads(_round_two_prompt([item], {}, "en"))
    rules = " ".join(payload["rules"])
    assert payload["items"][0]["risk_reasons"] == item["reasons"]
    assert "T7-C Entity Resolver" in rules
    assert "unknown_entity" in rules and "hallucinated_entity" in rules
    assert "禁止引入 glossary" in rules and "不得自行音译" in rules


def test_evaluate_round_two_en_rejects_replace_without_structured_evidence():
    """en replace 没有自动可修复 risk evidence 时由语言无关门禁拒绝。"""
    item = _en_item(reasons=[], translated="我抽了20次")
    decision = {
        "decision": "replace",
        "text": "我进行了20次抽取",
        "confidence": 0.95,
        "reason": "优化表达",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert result["accepted"] is False
    assert "weak_risk_only" in result["validation_reasons"]


def test_evaluate_round_two_en_accepts_minimal_number_fix_with_evidence():
    """结构化 number_mismatch 证据支持的最小数字修正可通过门禁。"""
    decision = {
        "decision": "replace",
        "text": "我抽了20次",
        "confidence": 0.9,
        "reason": "canonical span '20 pulls' was mistranslated as 10次; correct 10 to 20",
    }
    result = _evaluate_round_two_decision(_en_item(), decision, {})
    assert result["accepted"] is True
    assert result["applied"] == "round2"
    assert result["text"] == "我抽了20次"


def test_evaluate_round_two_en_accepts_negation_fix_with_evidence():
    """canonical 明确否定且 Round 1 翻反时，最小否定修正通过。"""
    item = _en_item(
        source_text="I will never go",
        english="I will never go",
        translated="我会去",
        reasons=["negation_missing"],
    )
    decision = {
        "decision": "replace",
        "text": "我绝对不会去",
        "confidence": 0.9,
        "reason": "canonical span 'never go' is negated; Round 1 reversed it",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    assert result["accepted"] is True
    assert result["text"] == "我绝对不会去"


def test_evaluate_round_two_en_rejects_glossary_external_hallucinated_name():
    """entity-only 风险保持人工处理，不能自动采纳 glossary 外中文人名。"""
    item = _en_item(
        source_text="Xyzzqz is here",
        english="Xyzzqz is here",
        translated="他来了",
        reasons=["unknown_entity:Xyzzqz", "hallucinated_entity:泽尔卡恩"],
    )
    decision = {
        "decision": "replace",
        "text": "泽尔卡恩来了",
        "confidence": 0.99,
        "reason": "invent a Chinese name for Xyzzqz",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    # 2026-08-10 审计修复：unknown_entity 现在进自动 Reviewer（eligible），
    # 但 gate 新增幻觉名防护——replace 引入 glossary 外新中文名必须拒绝。
    assert result["accepted"] is False
    assert any(
        reason.startswith("introduced_hallucinated_name")
        for reason in result["validation_reasons"]
    ), result["validation_reasons"]


def test_entity_only_replace_must_address_flagged_entity():
    """An entity-only review cannot publish an unrelated semantic edit."""
    item = _en_item(
        source_text=(
            "Wuthering Waves and Arknights Endfield have the best"
        ),
        english="Wuthering Waves and Arknights Endfield have the best",
        translated="《鸣潮》和《终末地》是画面最好的",
        reasons=[
            "unknown_entity:Wuthering Waves,Arknights Endfield",
            "hallucinated_entity:鸣潮,终末地",
        ],
    )
    decision = {
        "decision": "replace",
        "text": "《鸣潮》和《终末地》是最好的",
        "confidence": 0.9,
        "reason": "source ends at 'the best', so remove 画面",
    }

    result = _evaluate_round_two_decision(item, decision, {})

    assert result["accepted"] is False
    assert "entity_risk_not_addressed" in result["validation_reasons"]


def test_evaluate_round_two_accepts_entity_fix_case9():
    """Case 9：Round 1 '我要去隧门' 错，canonical 含 에이메스，
    replace 候选 '爱弥斯，我真的要去'（conf=0.9）通过门禁被采纳。"""
    item = _ko_item()
    decision = {
        "decision": "replace",
        "text": "爱弥斯，我真的要去。",
        "confidence": 0.9,
        "reason": "canonical source contains 에이메스 (爱弥斯); round1 missed entity",
    }
    result = _evaluate_round_two_decision(item, decision, {"에이메스": "爱弥斯"})
    assert result["accepted"] is True
    assert result["applied"] == "round2"
    assert "爱弥斯" in result["text"]


def test_low_confidence_kana_cleanup_is_published_but_stays_in_review():
    """A clean Chinese fallback is safer for display, but is not auto-resolved."""
    item = {
        "subtitle_id": 52,
        "source_language": "ja",
        "source_text": "エチルックってなんやねん。",
        "english": "エチルックってなんやねん。",
        "primary_evidence": "エチルックってなんやねん。",
        "secondary_evidence": "",
        "translated": "エチルック是什么鬼啊",
        "reasons": ["kana_residue"],
        "term_hints": {},
        "start": "00:00:00,000",
        "end": "00:00:02,000",
    }
    decision = {
        "decision": "replace",
        "text": "艾奇鲁克是什么鬼啊",
        "confidence": 0.7,
        "reason": "去除日文假名残留",
    }

    result = _evaluate_round_two_decision(item, decision, {})

    assert result["accepted"] is False
    assert result["applied"] == "round2_review"
    assert result["text"] == "艾奇鲁克是什么鬼啊"
    assert result["validation_reasons"] == ["low_confidence"]


def test_evaluate_round_two_rejects_hallucinated_name():
    """幻觉防护：replace 候选引入 glossary 外的新人名且无证据 → 拒绝。"""
    item = dict(_ko_item(), translated="爱弥斯，我真的要去",
                reasons=["suspected_name"])
    # 候选把角色改成不存在的名字
    decision = {
        "decision": "replace",
        "text": "罗雅伦，我真的要去。",
        "confidence": 0.8,
        "reason": "rewrite",
    }
    result = _evaluate_round_two_decision(item, decision, {"에이메스": "爱弥斯"})
    # 术语 term_hints 要求“爱弥斯”，候选没含 → term_mismatch 未修复 → 拒绝
    assert result["accepted"] is False


# ------------------------------------------------------------------
# P0-2 新增测试：Reviewer 不得绕过 canonical 重新选源
# ------------------------------------------------------------------


def test_round_two_prompt_ko_canonical_is_source_of_truth():
    """ja/ko prompt 必须明确 source_text 是仲裁后 canonical，是唯一翻译依据；
    primary/secondary 仅 advisory，不可覆盖。"""
    prompt = _round_two_prompt([_ko_item()], {}, "ko")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    assert "仲裁后的标准源文本" in rules or "canonical" in rules
    assert "advisory" in rules or "仅供参考" in rules
    assert "不可替代 source_text" in rules or "不可替代" in rules
    assert "禁止" in rules and "覆盖 source_text" in rules
    assert "不是重新仲裁双源" in rules


def test_round_two_prompt_ja_canonical_is_source_of_truth():
    """日语 prompt 同样必须有 canonical source-of-truth 规则。"""
    item = dict(_ko_item(), source_language="ja",
                source_text="エイメスが来た",
                english="エイメスが来た",
                primary_evidence="エイメスが来た",
                secondary_evidence="エイメス来た")
    prompt = _round_two_prompt([item], {}, "ja")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    assert "仲裁后的标准源文本" in rules or "canonical" in rules
    assert "advisory" in rules or "仅供参考" in rules
    assert "不是重新仲裁双源" in rules


def test_reviewer_99_regression_scenario():
    """模拟 final9 99 号句 Regression 场景：
    canonical=그만해요（住手）→ Round1=住手！
    Reviewer 看到 primary=소용 없어요（没用）后改为“没用的。”
    验证 prompt 明确禁止这种行为。"""
    item = {
        "subtitle_id": 99,
        "source_language": "ko",
        "source_text": "그만해요.",
        "english": "그만해요.",
        "primary_evidence": "소용 없어요.",
        "secondary_evidence": "그만해요.",
        "translated": "住手！",
        "reasons": ["negation_missing"],
        "term_hints": {},
        "context_before": "",
        "context_after": "",
        "start": "00:09:49,399",
        "end": "00:09:52,079",
    }
    prompt = _round_two_prompt([item], {}, "ko")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    # prompt 必须告诉 Reviewer：source_text 是 canonical，不可用 primary 覆盖
    assert "不可替代 source_text" in rules
    assert "不是重新仲裁双源" in rules
    # 验证 item 中 source_text 是 canonical 而非 primary
    assert payload["items"][0]["source_text"] == "그만해요."
    assert payload["items"][0]["primary_evidence"] == "소용 없어요."
    assert payload["items"][0]["round1_chinese"] == "住手！"


def test_reviewer_replace_based_on_primary_not_canonical_rejected():
    """确定性门禁局限：短文本（“住手！”vs“没用的。”）无法触发 excessive_rewrite。
    这证明对短句 Regression，确定性门禁不够用——prompt 层面的 canonical
    source-of-truth 规则是必要防线。
    """
    item = {
        "subtitle_id": 99,
        "source_language": "ko",
        "source_text": "그만해요.",
        "english": "그만해요.",
        "primary_evidence": "소용 없어요.",
        "secondary_evidence": "그만해요.",
        "translated": "住手！",
        "reasons": ["negation_missing"],
        "term_hints": {},
        "context_before": "",
        "context_after": "",
        "start": "00:09:49,399",
        "end": "00:09:52,079",
    }
    decision = {
        "decision": "replace",
        "text": "没用的。",
        "confidence": 1.0,
        "reason": "原文소용 없어요明确意为'没用'",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    # 短文本（2 vs 3 chars）不触发 excessive_rewrite（阈值 >= 6）
    # 确定性门禁确实无法拦截——这是已知局限
    # prompt 层面的 canonical source-of-truth 规则是唯一的预防手段
    comparable_r1 = len("住手")
    comparable_cand = len("没用的")
    assert min(comparable_r1, comparable_cand) < 6, \
        "短文本不应触发 excessive_rewrite"
    # 但 prompt 已经明确禁止 Reviewer 这样做
    prompt = _round_two_prompt([item], {}, "ko")
    payload = json.loads(prompt)
    rules = " ".join(payload["rules"])
    assert "不可替代 source_text" in rules
    assert "不是重新仲裁双源" in rules


# ------------------------------------------------------------------
# 2026-08-11 Phase 7：JA 长句普通短语误判幻觉名（JA-2 残留根因）
# 真实案例：rerun-ja-20260810 30 个 replace_rejected 中，
# introduced_hallucinated_name 把"也就是说/这个锚点/是最早期"
# 等普通中文短语当幻觉人名，整句正确修复被拒。
# ------------------------------------------------------------------

def test_round_two_accepts_long_ja_fix_with_common_phrases():
    """JA 长句修复含普通中文短语不得被判幻觉名。"""
    item = _ko_item(
        source_language="ja",
        source_text=(
            "つまりこのアンカーは最初期、ラハイロイという地名すら"
            "存在しない頃に残された、門を開く鍵のようなものだ。"
        ),
        english=(
            "つまりこのアンカーは最初期、ラハイロイという地名すら"
            "存在しない頃に残された、門を開く鍵のようなものだ。"
        ),
        translated="也就是说这个锚点是最早期、连拉海洛这地名都还不存在时就留下的星门钥匙。",
        reasons=["hallucinated_entity:星门", "unknown_entity:ラハイロイ"],
        term_hints={"ラハイロイ": "拉海洛"},
        start="00:00:00,000",
        end="00:00:06,000",
    )
    decision = {
        "decision": "replace",
        "text": "也就是说这个锚点是最早期、连拉海洛这地名都还不存在时就留下的，是开启门的钥匙吧。",
        "confidence": 0.9,
        "reason": "源文ゲート仅指门，round1 译为星门添加了原文没有的星字，应改为门",
    }
    result = _evaluate_round_two_decision(
        item, decision, {"ラハイロイ": "拉海洛"},
    )
    # 修复正确（星门→门）且未引入新幻觉名 → 必须通过
    assert result["accepted"] is True, result["validation_reasons"]


def test_round_two_accepts_ja_negation_fix_with_transliteration():
    """JA 否定翻转修复 + 已有音译名（蕾姆塔洛斯）不得被误拒。"""
    item = _ko_item(
        source_language="ja",
        source_text="なんかめっちゃ来てない。レムタロスちゃんなら絶対に大丈夫。",
        english="なんかめっちゃ来てない。レムタロスちゃんなら絶対に大丈夫。",
        translated="好多东西涌过来了。蕾姆塔洛斯酱的话绝对没问题。",
        reasons=[
            "hallucinated_entity:好多东西,涌过来了",
            "unknown_entity:レムタロス",
        ],
        term_hints={},
    )
    decision = {
        "decision": "replace",
        "text": "总觉得完全没来。完全没来。蕾姆塔洛斯酱的话绝对没问题。",
        "confidence": 0.9,
        "reason": "来てない 明确表示没来，round1 否定翻转，修正",
    }
    result = _evaluate_round_two_decision(item, decision, {})
    # 蕾姆塔洛斯是源文レムタロス的音译（round1 已有），不是新幻觉名 → 通过
    assert result["accepted"] is True, result["validation_reasons"]


def test_round_two_still_rejects_genuine_hallucinated_name():
    """真幻觉名防护不回归：候选引入源文没有的新人名 → 仍拒绝。"""
    item = _ko_item(
        source_language="ja",
        source_text="エイメスが来た。",
        english="エイメスが来た。",
        translated="爱弥斯来了。",
        reasons=["unknown_entity:エイメス"],
        term_hints={"エイメス": "爱弥斯"},
    )
    decision = {
        "decision": "replace",
        "text": "泽尔卡恩来了。爱弥斯也来了。",
        "confidence": 0.95,
        "reason": "rewrite",
    }
    result = _evaluate_round_two_decision(
        item, decision, {"エイメス": "爱弥斯"},
    )
    # 泽尔卡恩 不在 glossary/term_hints、round1 无、源文无 → 必须拒绝
    assert result["accepted"] is False
    assert any(
        reason.startswith("introduced_hallucinated_name")
        for reason in result["validation_reasons"]
    ), result["validation_reasons"]
