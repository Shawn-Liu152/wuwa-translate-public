import json

from tools.audit_translation_memory import audit_translation_memory


def _entry(entry_id, source, final, language="en", approved=True):
    return {
        "id": entry_id,
        "source": source,
        "final": final,
        "source_language": language,
        "target_language": "zh-CN",
        "approved": approved,
    }


def test_audit_counts_without_mutating_approval_and_finds_conflicts():
    memory = {
        "entries": [
            _entry("a", "Hello Rover", "你好，漂泊者！"),
            _entry("b", " hello rover ", "漂泊者，你好。"),
            _entry("c", "未批准", "待审核", language="ja", approved=False),
        ],
    }
    before = json.dumps(memory, sort_keys=True, ensure_ascii=False)

    report, candidates = audit_translation_memory(memory)

    assert report["total_entries"] == 3
    assert report["approved_by_language"] == {"en": 2}
    assert report["unapproved_by_language"] == {"ja": 1}
    assert report["normalized_duplicate_group_count"] == 1
    assert report["conflict_group_count"] == 1
    assert report["conflicts"][0]["tm_ids"] == ["a", "b"]
    assert any(
        item["tm_id"] == "c"
        and "awaiting_human_approval" in item["reasons"]
        for item in candidates
    )
    assert json.dumps(memory, sort_keys=True, ensure_ascii=False) == before


def test_audit_flags_objective_damage_pollution_length_and_stale_term():
    memory = {
        "entries": [
            _entry("polluted", "これは한글です", "污染", language="ja"),
            _entry("damaged", "bad\ufffdsource", "损坏"),
            _entry("long", "word " * 40, "过长"),
            _entry("stale", "Open the Strider Gate now", "打开隧门"),
        ],
    }
    evidence = {
        "glossary_actions": [{
            "operation": "replace",
            "source_language": "en",
            "old_source_term": "Strider Gate",
            "source_term": "Stridergate",
        }],
    }

    report, candidates = audit_translation_memory(
        memory, evidence=evidence, max_source_chars=160,
    )

    reasons = {item["tm_id"]: item["reasons"] for item in candidates}
    assert report["cross_language_pollution_count"] == 1
    assert report["source_damage_count"] == 1
    assert report["overlong_count"] == 1
    assert report["stale_official_term_count"] == 1
    assert "cross_language_script" in reasons["polluted"]
    assert "source_damaged" in reasons["damaged"]
    assert "overlong_example" in reasons["long"]
    assert "wrong_official_term" in reasons["stale"]


def test_audit_lists_confirmed_holdout_leakage_by_language():
    memory = {
        "entries": [
            _entry("tm-en", "Can you explain this?", "能解释一下吗？"),
            _entry("tm-ja", "これは別", "这是别的", language="ja"),
        ],
    }
    holdouts = {
        "en": {
            "entries": [{
                "id": "holdout-en",
                "canonical_source_text": "Can you explain this?",
                "gold_status": "confirmed",
            }],
        },
        "ja": {
            "entries": [{
                "id": "holdout-ja",
                "canonical_source_text": "一致しない",
                "gold_status": "confirmed",
            }],
        },
    }

    report, candidates = audit_translation_memory(memory, holdouts=holdouts)

    assert report["holdout_leakage"]["en"]["match_count"] == 1
    assert report["holdout_leakage"]["ja"]["match_count"] == 0
    leaked = next(item for item in candidates if item["tm_id"] == "tm-en")
    assert leaked["reasons"] == ["confirmed_holdout_leakage"]
    assert leaked["holdout_ids"] == ["holdout-en"]
