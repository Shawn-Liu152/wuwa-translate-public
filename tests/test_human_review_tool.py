import pytest
pytestmark = pytest.mark.skip(reason="Private review corpus is not distributed in the public repository")
import json
from pathlib import Path


def test_human_review_allowlists_are_explicit_and_safe():
    from tools.apply_human_review import CONFIRMED_GOLD, EN_MEMORY

    assert {lang: len(ids) for lang, ids in CONFIRMED_GOLD.items()} == {
        "en": 10, "ja": 10, "ko": 10,
    }
    assert len(EN_MEMORY) == 17
    assert len({source.casefold() for source, _ in EN_MEMORY}) == 17
    assert all(source.strip() and final.strip() for source, final in EN_MEMORY)


def test_human_review_tool_skips_an_already_approved_exact_pair():
    source = Path(__file__).resolve().parents[1].joinpath(
        "tools", "apply_human_review.py"
    ).read_text(encoding="utf-8")
    assert "if (normalize_source(source, \"en\"), final) in existing:" in source
    assert "continue" in source


def test_checked_in_human_review_results_exist():
    root = Path(__file__).resolve().parents[1]
    for language in ("en", "ja", "ko"):
        payload = json.loads(
            (root / "benchmarks" / f"{language}_reaction_benchmark.json")
            .read_text(encoding="utf-8")
        )
        assert sum(
            entry.get("gold_status") == "confirmed"
            for entry in payload["entries"]
        ) >= 10
    tm = json.loads(
        (root / "data" / "translation_memory.json").read_text(encoding="utf-8")
    )
    assert sum(
        item.get("approved") and item.get("source_language") == "en"
        for item in tm["entries"]
    ) >= 50
