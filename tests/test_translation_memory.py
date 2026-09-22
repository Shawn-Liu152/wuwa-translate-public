from pipeline.translation_memory import TranslationMemoryStore


def test_translation_memory_deduplicates_and_requires_explicit_approval(tmp_path):
    store = TranslationMemoryStore(tmp_path / "memory.json")

    first = store.record_edit(
        source="  Phoebe   is here. ",
        previous="菲贝来了",
        final="菲比来了",
        game="wuwa",
        error_type="人名",
        job_id="job-a",
        risk_key="risk-1",
    )
    assert first["approved"] is False
    assert store.approved_for_sources(["Phoebe is here."]) == []

    second = store.record_edit(
        source="Phoebe is here.",
        previous="菲贝来了",
        final="菲比来了",
        game="wuwa",
        error_type="人名",
        job_id="job-b",
        risk_key="risk-2",
        approve=True,
    )

    assert second["id"] == first["id"]
    assert second["approved"] is True
    assert second["seen_count"] == 2
    assert store.approved_for_sources(["Phoebe is here."])[0]["final"] == "菲比来了"


def test_translation_memory_never_reuses_a_different_source(tmp_path):
    store = TranslationMemoryStore(tmp_path / "memory.json")
    store.record_edit(
        source="Phoebe is here.", previous="旧", final="新",
        game="wuwa", error_type="人名", job_id="job-a",
        risk_key="risk-1", approve=True,
    )

    assert store.approved_for_sources(["Phoebe has arrived."]) == []


def test_translation_memory_isolated_by_language_pair_and_migrates_legacy(tmp_path):
    store = TranslationMemoryStore(tmp_path / "memory.json")
    store.record_edit(
        source="カルテジア", previous="旧", final="卡提希娅",
        game="wuwa", error_type="人名", job_id="ja-job", risk_key="ja-risk",
        approve=True, source_language="ja", target_language="zh-CN",
    )

    assert store.approved_for_sources(
        ["カルテジア"], game="wuwa", source_language="en",
    ) == []
    assert store.approved_for_sources(
        ["カルテジア"], game="wuwa", source_language="ja",
    )[0]["final"] == "卡提希娅"


def test_korean_translation_memory_never_leaks_into_japanese(tmp_path):
    store = TranslationMemoryStore(tmp_path / "memory.json")
    store.record_edit(
        source="카르테시아", previous="旧", final="卡提希娅",
        game="wuwa", error_type="人名", job_id="ko-job", risk_key="ko-risk",
        approve=True, source_language="ko", target_language="zh-CN",
    )

    assert store.approved_for_sources(
        ["카르테시아"], game="wuwa", source_language="ja",
    ) == []
    assert store.approved_for_sources(
        ["카르테시아"], game="wuwa", source_language="ko",
    )[0]["final"] == "卡提希娅"
