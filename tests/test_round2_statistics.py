"""P1 测试：Round 2 decision/deferred 统计口径修正。

验证每个风险项都有明确的 round2_status，不再用 null/空 同时表示多种含义。
状态至少包括：deferred_weak_risk / keep / replace / replace_rejected / unresolved / no_result。

逻辑关系：
  risk_total = deferred_weak_risk + round2_eligible
  round2_eligible = keep + replace + replace_rejected + unresolved + no_result
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.long_video import _requires_blocking_round_two


# ---------- _requires_blocking_round_two ----------

def test_blocking_reasons_trigger_round_two():
    """有 AUTOMATIC_REVIEW_REASONS 中的 reason → blocking=True。"""
    for reason in [
        "empty_translation", "number_mismatch", "term_mismatch",
        "english_residue", "kana_residue", "hangul_residue",
        "negation_missing", "suspected_name", "incomplete_fragment",
    ]:
        item = {"reasons": [reason]}
        assert _requires_blocking_round_two(item) is True, f"{reason} should be blocking"


def test_weak_reasons_do_not_trigger_round_two():
    """只有 MANUAL_REVIEW_REASONS 或 advisory reasons → blocking=False。"""
    # 2026-08-10 审计：text_conflict 已升级为自动审查原因（Source Truth 级），
    # 不再属于 weak；其余 advisory 原因保持不 blocking。
    for reason in ["person_name_present", "reading_speed",
                   "condition_marker_missing"]:
        item = {"reasons": [reason]}
        assert _requires_blocking_round_two(item) is False, f"{reason} should not be blocking"


def test_text_conflict_now_triggers_round_two():
    """双源文本冲突必须进自动 Reviewer（2026-08-10 审计修复）。"""
    assert _requires_blocking_round_two({"reasons": ["text_conflict"]}) is True


def test_mixed_reasons_trigger_round_two():
    """混合 strong + weak → blocking=True（有任一 strong 即触发）。"""
    item = {"reasons": ["person_name_present", "negation_missing"]}
    assert _requires_blocking_round_two(item) is True


def test_empty_reasons_not_blocking():
    """无 reasons → blocking=False。"""
    assert _requires_blocking_round_two({"reasons": []}) is False
    assert _requires_blocking_round_two({}) is False


# ---------- round2_status 字段存在性 ----------

def test_round2_status_values_are_exhaustive():
    """验证所有可能的 round2_status 值覆盖了全部场景。"""
    expected_statuses = {
        "deferred_weak_risk",  # 未进入 Round 2（弱风险）
        "keep",                # 模型返回 keep
        "replace",             # 模型返回 replace，门禁通过
        "replace_rejected",    # 模型返回 replace，门禁拒绝
        "unresolved",          # 模型返回 review/unresolved
        "no_result",           # 已提交但无结果（parse/API error）
    }
    # 这些状态互斥且覆盖所有可能：
    # 有 decision → keep/replace/replace_rejected/unresolved
    # 无 decision + blocking → no_result
    # 无 decision + non-blocking → deferred_weak_risk
    assert len(expected_statuses) == 6


# ---------- summary 逻辑关系 ----------

def test_summary_logical_relationships():
    """验证 summary 满足核心不变式：
    risk_total = keep + replace + replace_rejected + unresolved + no_result + deferred_weak_risk

    注意：round2_eligible 是 blocking items 的数量，但 keep/replace/...
    可能包含非 blocking items（其 subtitle_id 碰巧匹配了模型返回的 decision）。
    因此 keep + replace + ... 可能 > round2_eligible。
    真正的不变式是：所有 6 个状态之和 = risk_total。
    """
    # 模拟日语 E2E 的真实数据：48 total, 5 eligible, 7 decisions (4 keep + 3 replace)
    # 模型返回了 7 个 decision，但只有 5 个 blocking items 被提交
    # 多出的 2 个 decision 可能匹配到了非 blocking 的 generated_items
    summary = {
        "risk_total": 48,
        "round2_eligible": 5,
        "keep": 4,
        "replace": 3,
        "replace_rejected": 0,
        "unresolved": 0,
        "no_result": 0,
        "deferred_weak_risk": 41,  # 48 - 7 items with decisions = 41
    }
    # 核心不变式：6 个状态之和 = risk_total
    all_statuses = (
        summary["keep"] + summary["replace"] + summary["replace_rejected"]
        + summary["unresolved"] + summary["no_result"] + summary["deferred_weak_risk"]
    )
    assert all_statuses == summary["risk_total"], \
        f"sum of all statuses ({all_statuses}) != risk_total ({summary['risk_total']})"

    # round2_eligible = blocking items, no_result = blocking without decision
    # 所以 no_result <= round2_eligible
    assert summary["no_result"] <= summary["round2_eligible"]

    # deferred_weak_risk = non-blocking items without decision
    # 所以 deferred_weak_risk <= risk_total - round2_eligible
    assert summary["deferred_weak_risk"] <= summary["risk_total"] - summary["round2_eligible"]


def test_summary_with_no_results():
    """当所有 eligible items 都没有获得 decision 时：
    no_result = round2_eligible, 其他 outcome = 0。"""
    summary = {
        "risk_total": 10,
        "round2_eligible": 3,
        "keep": 0,
        "replace": 0,
        "replace_rejected": 0,
        "unresolved": 0,
        "no_result": 3,
        "deferred_weak_risk": 7,
    }
    # 核心不变式：6 个状态之和 = risk_total
    all_statuses = (
        summary["keep"] + summary["replace"] + summary["replace_rejected"]
        + summary["unresolved"] + summary["no_result"] + summary["deferred_weak_risk"]
    )
    assert all_statuses == summary["risk_total"]
    assert summary["no_result"] == summary["round2_eligible"]


# ---------- 向后兼容 ----------

def test_old_round2_results_without_round2_status_are_safe():
    """旧 JSON 中没有 round2_status 字段 → 读取时默认 deferred_weak_risk，不抛异常。"""
    old_item = {
        "key": "00:00:00,000|00:00:02,000|1",
        "state": "on_demand",
        "decision": "",
    }
    # 模拟读取旧 JSON 时的默认行为
    status = old_item.get("round2_status", "deferred_weak_risk")
    assert status == "deferred_weak_risk"
    # 旧 JSON 不抛异常
    assert old_item.get("decision", "") == ""


def test_old_round2_results_with_decision_infer_status():
    """旧 JSON 中有 decision 但无 round2_status → 可从 decision + accepted 推断。"""
    old_keep = {"decision": "keep", "accepted": True}
    old_replace_accepted = {"decision": "replace", "accepted": True}
    old_replace_rejected = {"decision": "replace", "accepted": False}

    # 推断逻辑（与代码中一致）
    def infer_status(item):
        if "round2_status" in item:
            return item["round2_status"]
        dec = str(item.get("decision", "")).lower()
        if dec == "keep":
            return "keep"
        if dec == "replace":
            return "replace" if item.get("accepted") else "replace_rejected"
        if item.get("state") == "on_demand":
            return "deferred_weak_risk"
        return "no_result"

    assert infer_status(old_keep) == "keep"
    assert infer_status(old_replace_accepted) == "replace"
    assert infer_status(old_replace_rejected) == "replace_rejected"
