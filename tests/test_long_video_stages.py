import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import long_video
from pipeline.candidates import EnglishCandidate
from pipeline.parser.srt_parser import Subtitle, parse_srt, save_srt
from pipeline.pipeline_manifest import load_manifest
from pipeline.translate.llm import LLMTransientError
from pipeline.risk_queue import RiskItem
from pipeline.risk_queue_store import (
    load_round2_results,
    save_generated_risks,
    save_round2_results,
)


def test_stage_paths_keep_round_outputs_independent(tmp_path):
    paths = long_video._stage_paths(str(tmp_path / "final.zh.srt"))

    assert paths["round1"].endswith("final.round1.zh.srt")
    assert paths["round2"].endswith("final.round2.zh.srt")
    assert paths["round1_manifest"] != paths["round2_manifest"]
    assert paths["final"].endswith("final.zh.srt")
    assert paths["risk_generated"].endswith("final.zh.risk.generated.json")
    assert paths["round2_results"].endswith("final.zh.round2.results.json")
    assert paths["review_state"].endswith("final.zh.review-state.json")


def test_blocking_risk_keys_accept_generated_risk_objects():
    blocking = RiskItem(
        key="blocking",
        subtitle_id=1,
        start="00:00:00,000",
        end="00:00:02,000",
        score=10,
        reasons=["empty_translation"],
        english="Welcome back.",
        capcut_en="Welcome back.",
        whisper_en="",
        translated="",
        review_required=True,
    )
    advisory = RiskItem(
        key="advisory",
        subtitle_id=2,
        start="00:00:02,000",
        end="00:00:04,000",
        score=2,
        reasons=["reading_speed"],
        english="That was fast.",
        capcut_en="That was fast.",
        whisper_en="",
        translated="太快了。",
    )

    assert long_video._blocking_risk_keys([blocking, advisory]) == {"blocking"}


def test_final_publish_cleans_output_without_changing_round_two(tmp_path):
    round_two = tmp_path / "final.round2.zh.srt"
    final = tmp_path / "final.zh.srt"
    save_srt(str(round_two), [
        Subtitle(1, "00:00:00,000", "00:00:02,000", r"你好 [\h__\h]"),
        Subtitle(2, "00:00:01,900", "00:00:03,000", "你好。"),
    ])
    stage_bytes = round_two.read_bytes()

    long_video._publish_final(str(round_two), str(final))

    assert round_two.read_bytes() == stage_bytes
    assert [(item.id, item.text) for item in parse_srt(str(final))] == [
        (1, "你好"),
    ]


def test_final_publish_removes_sentence_final_full_stop(tmp_path):
    round_two = tmp_path / "final.round2.zh.srt"
    final = tmp_path / "final.zh.srt"
    save_srt(str(round_two), [
        Subtitle(1, "00:00:00,000", "00:00:02,000", "版本3.1上线了。"),
    ])

    long_video._publish_final(str(round_two), str(final))

    assert parse_srt(str(final))[0].text == "版本3.1上线了"


def test_final_publish_redistributes_semantic_translation_from_display_map(tmp_path):
    round_two = tmp_path / "final.round2.zh.srt"
    final = tmp_path / "final.zh.srt"
    display_map = tmp_path / "display-map.json"
    save_srt(str(round_two), [Subtitle(
        1, "00:00:00,000", "00:00:12,000",
        "我甚至不知道她当时到底想做什么，不过那个决定真的很奇怪。",
    )])
    display_map.write_text(json.dumps({"1": [
        {"start": "00:00:00,000", "end": "00:00:03,000"},
        {"start": "00:00:03,000", "end": "00:00:07,000"},
        {"start": "00:00:07,000", "end": "00:00:12,000"},
    ]}), encoding="utf-8")

    long_video._publish_final(str(round_two), str(final), str(display_map))

    published = parse_srt(str(final))
    assert len(published) >= 2
    assert "".join(item.text for item in published) == (
        "我甚至不知道她当时到底想做什么，不过那个决定真的很奇怪"
    )
    assert published[0].start == "00:00:00,000"
    assert published[-1].end == "00:00:12,000"


def test_final_publish_splits_a_long_dense_cue_without_a_display_map(tmp_path):
    round_two = tmp_path / "final.round2.zh.srt"
    final = tmp_path / "final.zh.srt"
    text = "这是一条持续时间过长而且内容非常密集的字幕，需要稳定拆成几条更容易阅读的字幕"
    save_srt(str(round_two), [
        Subtitle(1, "00:00:00,000", "00:00:14,000", text),
    ])

    long_video._publish_final(str(round_two), str(final))

    published = parse_srt(str(final))
    assert len(published) >= 3
    assert "".join(item.text for item in published) == text
    assert published[0].start == "00:00:00,000"
    assert published[-1].end == "00:00:14,000"


def test_union_merges_meaningful_micro_cue_before_translation(tmp_path):
    primary = tmp_path / "youtube.en.srt"
    secondary = tmp_path / "empty.en.srt"
    union = tmp_path / "union.en.srt"
    save_srt(str(primary), [
        Subtitle(
            30,
            "00:01:31,040",
            "00:01:34,270",
            "I think I just heard aspirations for",
        ),
        Subtitle(
            31,
            "00:01:34,270",
            "00:01:34,280",
            "greatness.",
        ),
    ])
    secondary.write_text("", encoding="utf-8")

    candidates, primary_count, secondary_count = long_video._write_union(
        str(primary), str(secondary), str(union)
    )

    assert primary_count == 2
    assert secondary_count == 0
    assert len(candidates) == 1
    assert [(item.start, item.end, item.text) for item in parse_srt(str(union))] == [
        (
            "00:01:31,040",
            "00:01:34,280",
            "I think I just heard aspirations for greatness.",
        ),
    ]


def test_review_glossary_excludes_soft_gameplay_words():
    glossary = long_video._load_review_glossary("wuwa")
    matched = long_video._add_fuzzy_name_hints(
        glossary, "Weapon Support, take Cover, this Brand, and Chagli", "wuwa"
    )

    assert glossary["Changli"] == "长离"
    assert glossary["Rover"] == "漂泊者"
    assert "Weapon" not in glossary
    assert "Support" not in glossary
    assert "Weapon" not in matched
    assert "Cover" not in matched
    assert "Brand" not in matched
    assert matched["Chagli"] == "长离"


def test_person_name_review_does_not_trigger_round_two_by_itself():
    item = {
        "reasons": ["person_name_present"],
        "review_required": True,
    }

    assert long_video._requires_blocking_round_two(item) is False


def test_review_glossary_matches_safe_multiword_alias_across_line_break():
    matched = long_video._add_fuzzy_name_hints(
        {},
        "That looked like Levi versus\nKenny.",
        "wuwa",
    )

    assert matched["Levi"] == "利威尔"
    assert matched["Levi versus Kenny"] == "利威尔对肯尼"


def test_risk_evidence_includes_secondary_english_for_name_detection():
    candidate = EnglishCandidate(
        key="secondary-name",
        start="00:00:00,000",
        end="00:00:02,000",
        capcut_en="Denny is here",
        whisper_en="Denia is here",
        source="capcut",
        flags=["text_conflict"],
    )

    evidence = long_video._all_english_risk_evidence([candidate])

    assert "Denny is here" in evidence
    assert "Denia is here" in evidence


def test_source_matched_person_aliases_do_not_add_unseen_aliases(tmp_path):
    (tmp_path / "alias.json").write_text(
        json.dumps({"Denny": "Denia", "Unseen": "Denia"}),
        encoding="utf-8",
    )

    aliases = long_video._source_matched_person_aliases(
        {"Denia": "达妮娅"},
        "Denny is here",
        str(tmp_path),
    )

    assert aliases == {"Denny": "达妮娅"}


def test_round_two_uses_strict_json_and_its_own_manifest(tmp_path, monkeypatch):
    class Translator:
        def _call_api(self, prompt):
            payload = json.loads(prompt)
            assert payload["output_schema"]["results"]
            assert payload["items"][0]["id"] == 7
            return json.dumps(
                {"results": [{
                    "id": 7, "decision": "replace", "text": "修正译文",
                    "confidence": 0.96, "reason": "术语错误",
                }]},
                ensure_ascii=False,
            ), 0, 0

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    queue_path = tmp_path / "risk-queue.json"
    queue_path.write_text('{"items":[]}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    items = [{
        "key": "risk-7", "subtitle_id": 7, "english": "Fix this",
        "capcut_en": "Fix this", "whisper_en": "Fix this",
        "translated": "旧译文", "reasons": ["term_mismatch"],
        "state": "pending",
    }]

    corrections = long_video._round_two(
        items, "test", "key", "wuwa",
        manifest_path=str(manifest_path),
        input_path=str(queue_path),
    )

    assert corrections[7]["text"] == "修正译文"
    assert corrections[7]["decision"] == "replace"
    manifest = load_manifest(str(manifest_path))
    assert manifest["status"] == "completed"
    assert manifest["options"]["stage"] == "round2"
    assert manifest["batches"]["0"]["state"] == "success"


def test_round_two_failure_redacts_untrusted_error_from_manifest_and_logs(
        tmp_path, monkeypatch, caplog):
    malicious = (
        "Authorization: Bearer sk-live-round-two Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )

    class Translator:
        def _call_api(self, prompt):
            raise RuntimeError(malicious)

    monkeypatch.setattr(long_video, "_load_glossary", lambda *args, **kwargs: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    queue_path = tmp_path / "risk.generated.json"
    queue_path.write_text('{"items":[]}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    items = [{
        "key": "risk-7", "subtitle_id": 7, "english": "Fix this",
        "capcut_en": "Fix this", "whisper_en": "", "translated": "旧译文",
        "reasons": ["term_mismatch"],
    }]

    assert long_video._round_two(
        items, "test", "key", "wuwa",
        manifest_path=str(manifest_path), input_path=str(queue_path),
    ) == {}

    payload = load_manifest(str(manifest_path))
    exposed = manifest_path.read_text(encoding="utf-8") + caplog.text
    for secret in (
        "sk-live-round-two",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
        str(tmp_path),
    ):
        assert secret not in exposed
    assert payload["input"]["path"] == "risk.generated.json"
    assert payload["batches"]["0"]["error_code"] == "round2_invalid_response"


def test_round_two_keeps_partial_results_and_retries_only_missing(
        tmp_path, monkeypatch):
    prompts = []

    class Translator:
        def _call_api(self, prompt):
            payload = json.loads(prompt)
            prompts.append([item["id"] for item in payload["items"]])
            if len(prompts) == 1:
                return json.dumps({
                    "results": [{
                        "id": 7, "decision": "replace", "text": "七号译文",
                        "confidence": 0.95, "reason": "术语错误",
                    }],
                }, ensure_ascii=False), 10, 2
            return json.dumps({
                "results": [{
                    "id": 8, "decision": "keep", "text": "八号译文",
                    "confidence": 0.92, "reason": "原译正确",
                }],
            }, ensure_ascii=False), 5, 2

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    queue_path = tmp_path / "risk.generated.json"
    queue_path.write_text('{"items":[]}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    items = [
        {
            "key": f"risk-{subtitle_id}",
            "subtitle_id": subtitle_id,
            "english": f"Fix {subtitle_id}",
            "capcut_en": f"Fix {subtitle_id}",
            "whisper_en": "",
            "translated": f"旧译文{subtitle_id}",
            "reasons": ["term_mismatch"],
        }
        for subtitle_id in (7, 8)
    ]

    corrections = long_video._round_two(
        items, "test", "key", "wuwa",
        manifest_path=str(manifest_path),
        input_path=str(queue_path),
    )

    assert corrections[7]["text"] == "七号译文"
    assert corrections[8]["decision"] == "keep"
    assert prompts == [[7, 8], [8]]
    manifest = load_manifest(str(manifest_path))
    assert manifest["options"]["provider"] == {
        "name": "unknown",
        "hostname": "",
    }
    assert manifest["batches"]["0"]["state"] == "success"
    assert manifest["batches"]["0"]["metrics"]["response_attempts"] == 2


def test_round_two_retries_keep_that_leaves_japanese_residue(
        tmp_path, monkeypatch):
    prompts = []

    class Translator:
        def _call_api(self, prompt):
            payload = json.loads(prompt)
            prompts.append([item["id"] for item in payload["items"]])
            if len(prompts) == 1:
                return json.dumps({
                    "results": [{
                        "id": 52, "decision": "keep",
                        "text": "エチルック到底是什么鬼啊",
                        "confidence": 0.8, "reason": "保留日文专名",
                    }],
                }, ensure_ascii=False), 10, 2
            return json.dumps({
                "results": [{
                    "id": 52, "decision": "replace",
                    "text": "埃奇鲁克到底是什么鬼啊",
                    "confidence": 0.95, "reason": "消除日文假名残留",
                }],
            }, ensure_ascii=False), 5, 2

    monkeypatch.setattr(long_video, "_load_glossary", lambda *args, **kwargs: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    queue_path = tmp_path / "risk.generated.json"
    queue_path.write_text('{"items":[]}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    items = [{
        "key": "risk-52", "subtitle_id": 52,
        "source_language": "ja", "source_text": "エチルックってなんやねん。",
        "primary_evidence": "エチルックってなんやねん。",
        "secondary_evidence": "", "translated": "エチルック到底是什么鬼啊",
        "reasons": ["kana_residue"], "start": "00:00:00,000",
        "end": "00:00:02,000",
    }]

    corrections = long_video._round_two(
        items, "test", "key", "wuwa", source_language="ja",
        manifest_path=str(manifest_path), input_path=str(queue_path),
    )

    assert prompts == [[52], [52]]
    assert corrections[52]["decision"] == "replace"
    assert corrections[52]["text"] == "埃奇鲁克到底是什么鬼啊"


def _round_two_items(count):
    return [
        {
            "key": f"risk-{subtitle_id}",
            "subtitle_id": subtitle_id,
            "english": f"Fix {subtitle_id}",
            "capcut_en": f"Fix {subtitle_id}",
            "whisper_en": "",
            "translated": f"旧译文 {subtitle_id}",
            "reasons": ["term_mismatch"],
        }
        for subtitle_id in range(1, count + 1)
    ]


def test_round_two_runs_two_batches_concurrently_with_one_translator(monkeypatch):
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    factory_calls = 0
    factory_options = []

    class Translator:
        def preflight(self):
            pass

        def _call_api(self, prompt):
            nonlocal active, maximum_active
            payload = json.loads(prompt)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return json.dumps({
                "results": [{
                    "id": item["id"],
                    "decision": "keep",
                    "text": item["round1_chinese"],
                    "confidence": 0.95,
                    "reason": "原译准确",
                } for item in payload["items"]],
            }, ensure_ascii=False), 1, 1

    def factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        factory_options.append(kwargs)
        return Translator()

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", factory)

    decisions = long_video._round_two(
        _round_two_items(25), "test", "key", "wuwa",
        proxy="http://127.0.0.1:7890",
    )

    assert len(decisions) == 25
    assert maximum_active == 2
    assert factory_calls == 1
    assert factory_options[0]["proxy"] == "http://127.0.0.1:7890"


def test_round_two_429_downgrades_remaining_work_to_serial(
        tmp_path, monkeypatch):
    queue_path = tmp_path / "risk.generated.json"
    queue_path.write_text('{"items":[]}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    first_call = True
    lock = threading.Lock()

    class Translator:
        def preflight(self):
            pass

        def _call_api(self, prompt):
            nonlocal first_call
            payload = json.loads(prompt)
            with lock:
                should_limit = first_call
                first_call = False
            if should_limit:
                raise LLMTransientError("API 429: limited", status_code=429)
            return json.dumps({
                "results": [{
                    "id": item["id"],
                    "decision": "keep",
                    "text": item["round1_chinese"],
                    "confidence": 0.95,
                    "reason": "原译准确",
                } for item in payload["items"]],
            }, ensure_ascii=False), 1, 1

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())

    decisions = long_video._round_two(
        _round_two_items(25), "test", "key", "wuwa",
        manifest_path=str(manifest_path),
        input_path=str(queue_path),
    )

    assert len(decisions) == 25
    manifest = load_manifest(str(manifest_path))
    assert manifest["status"] == "completed"
    assert manifest["metrics"]["parallelism"] == 1
    assert manifest["metrics"]["rate_limit_downgraded"] is True


def test_only_strong_risks_block_round_two():
    assert long_video._requires_blocking_round_two({
        "reasons": ["term_mismatch"],
    })
    # 2026-08-10 审计：text_conflict 已升级为自动审查原因（Source Truth 级），
    # reading_speed 单独仍不 blocking。
    assert not long_video._requires_blocking_round_two({
        "reasons": ["reading_speed"],
    })
    assert long_video._requires_blocking_round_two({
        "reasons": ["reading_speed", "text_conflict"],
    })


def test_round_two_rebases_changed_queue_and_preserves_matching_results(
        tmp_path, monkeypatch):
    old_queue = tmp_path / "old-risk.generated.json"
    new_queue = tmp_path / "new-risk.generated.json"
    old_queue.write_text('{"version":"old"}', encoding="utf-8")
    new_queue.write_text('{"version":"new"}', encoding="utf-8")
    manifest_path = tmp_path / "round2.manifest.json"
    old_items = [{
        "key": "stable-7", "subtitle_id": 7, "english": "Seven",
        "capcut_en": "Seven", "whisper_en": "", "translated": "旧七",
        "reasons": ["term_mismatch"],
    }]
    options = {
        "stage": "round2",
        "schema": "subtitle-corrections-v1",
        "model": "test",
        "game": "wuwa",
        "batch_size": long_video.ROUND_TWO_BATCH_SIZE,
        "evidence_fingerprint": "",
    }
    old_manifest = long_video._create_round_two_manifest(
        str(old_queue), options, old_items, {7: "已复核七"}
    )
    from pipeline.pipeline_manifest import save_manifest
    save_manifest(str(manifest_path), old_manifest)
    prompts = []

    class Translator:
        def _call_api(self, prompt):
            payload = json.loads(prompt)
            prompts.append([item["id"] for item in payload["items"]])
            return json.dumps({
                "results": [{
                    "id": 8, "decision": "replace", "text": "新复核八",
                    "confidence": 0.9, "reason": "术语错误",
                }],
            }, ensure_ascii=False), 0, 0

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    new_items = [
        old_items[0],
        {
            "key": "stable-8", "subtitle_id": 8, "english": "Eight",
            "capcut_en": "Eight", "whisper_en": "", "translated": "旧八",
            "reasons": ["term_mismatch"],
        },
    ]

    corrections = long_video._round_two(
        new_items, "test", "key", "wuwa",
        manifest_path=str(manifest_path),
        input_path=str(new_queue),
        resume=True,
    )

    assert corrections[7]["decision"] == "review"
    assert corrections[8]["text"] == "新复核八"
    assert prompts == [[8]]
    assert manifest_path.with_suffix(
        manifest_path.suffix + ".before-rebase.bak"
    ).exists()


def test_round_two_gate_keeps_weak_risk_and_accepts_verified_replace():
    weak = {
        "english": "A slightly long subtitle",
        "translated": "一条稍长的字幕",
        "reasons": ["reading_speed"],
        "start": "00:00:00,000",
        "end": "00:00:03,000",
    }
    replacement = {
        "decision": "replace",
        "text": "更短字幕",
        "confidence": 0.99,
        "reason": "缩短字幕",
    }
    rejected = long_video._evaluate_round_two_decision(
        weak, replacement, {}
    )
    assert rejected["accepted"] is False
    assert "weak_risk_only" in rejected["validation_reasons"]

    strong = {
        **weak,
        "english": "Rover is not here",
        "translated": "漂泊者在这里",
        "reasons": ["semantic_marker_missing"],
    }
    accepted = long_video._evaluate_round_two_decision(
        strong,
        {
            "decision": "replace",
            "text": "漂泊者不在这里",
            "confidence": 0.96,
            "reason": "补回否定含义",
        },
        {"Rover": "漂泊者"},
    )
    assert accepted["accepted"] is True
    assert accepted["applied"] == "round2"


def test_round_two_prompt_includes_context_and_required_name():
    prompt = json.loads(long_video._round_two_prompt([{
        "subtitle_id": 7,
        "english": "It is Chagli",
        "capcut_en": "It is Chagli",
        "whisper_en": "It is Changli",
        "translated": "是查格丽",
        "reasons": ["term_mismatch"],
        "term_hints": {"Chagli": "长离"},
        "context_before": "Who is next?",
        "context_after": "She looks amazing",
    }], {"Chagli": "长离"}))

    item = prompt["items"][0]
    assert item["required_name_terms"] == {"Chagli": "长离"}
    assert item["context_before"] == "Who is next?"
    assert item["context_after"] == "She looks amazing"
    assert "最小修正" in prompt["task"]
    joined_rules = "\n".join(prompt["rules"])
    assert "默认 keep" in joined_rules
    assert "pulling for" in joined_rules
    assert "incomplete_fragment" in joined_rules


def test_korean_round_two_prompt_never_reuses_english_specific_rules():
    prompt = json.loads(long_video._round_two_prompt([{
        "subtitle_id": 3,
        "source_text": "카르테시아는 지지 않아요",
        "primary_evidence": "카르테시아는 지지 않아요",
        "secondary_evidence": "카르테시아는 패배하지 않아요",
        "translated": "卡提希娅不会输",
        "reasons": ["person_name_present"],
        "term_hints": {"카르테시아": "卡提希娅"},
    }], {"카르테시아": "卡提希娅"}, source_language="ko"))

    assert "韩语到简体中文" in prompt["task"]
    assert any("敬语" in rule for rule in prompt["rules"])
    assert not any("英文证据" in rule for rule in prompt["rules"])


def test_round_two_keep_cannot_silently_resolve_known_name_error():
    outcome = long_video._evaluate_round_two_decision(
        {
            "english": "It is Chagli",
            "translated": "是查格丽",
            "reasons": ["term_mismatch"],
            "term_hints": {"Chagli": "长离"},
        },
        {
            "decision": "keep",
            "text": "是查格丽",
            "confidence": 0.99,
            "reason": "原译准确",
        },
        {},
    )

    assert outcome["accepted"] is False
    assert outcome["applied"] == "round1"
    assert "keep_does_not_fix:term_mismatch" in outcome["validation_reasons"]


def test_round_two_gate_rejects_low_confidence_and_mojibake():
    item = {
        "english": "Fix this",
        "translated": "旧译文",
        "reasons": ["empty_translation"],
        "start": "00:00:00,000",
        "end": "00:00:02,000",
    }
    outcome = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "ÎÞÊýµÄ¿ÉÄÜ",
            "confidence": 0.6,
            "reason": "重新翻译",
        },
        {},
    )
    assert outcome["accepted"] is False
    assert "low_confidence" in outcome["validation_reasons"]
    assert "mojibake" in outcome["validation_reasons"]


def test_round_two_gate_strips_reintroduced_music_annotation():
    item = {
        "english": "I have not seen this girl.",
        "translated": "\u6211\u6ca1\u89c1\u8fc7\u8fd9\u4e2a\u5973\u5b69\u3002",
        "reasons": ["semantic_marker_missing"],
        "start": "00:00:00,000",
        "end": "00:00:03,000",
    }
    outcome = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "\u6211\u4ece\u6ca1\u89c1\u8fc7\u8fd9\u4e2a\u5973\u5b69\u3002\uff08\u97f3\u4e50\uff09",
            "confidence": 0.99,
            "reason": "\u8865\u56de\u5426\u5b9a\u542b\u4e49",
        },
        {},
    )
    assert outcome["accepted"] is True
    assert outcome["text"] == "\u6211\u4ece\u6ca1\u89c1\u8fc7\u8fd9\u4e2a\u5973\u5b69\u3002"


def test_round_two_gate_requires_fragment_replacement_to_finish_the_sentence():
    item = {
        "english": "I don't even have a",
        "translated": "我甚至都没有一个",
        "reasons": ["incomplete_fragment"],
        "start": "00:00:00,000",
        "end": "00:00:02,000",
    }
    rejected = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "我甚至都没有一个",
            "confidence": 0.99,
            "reason": "已经正确",
        },
        {},
    )
    accepted = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "我甚至连这个角色都没有",
            "confidence": 0.96,
            "reason": "结合上下文补全残句",
        },
        {},
    )

    assert rejected["accepted"] is False
    assert "replacement_unchanged" in rejected["validation_reasons"]
    assert "incomplete_fragment_validation_failed" in rejected[
        "validation_reasons"
    ]
    assert accepted["accepted"] is True
    assert accepted["applied"] == "round2"


def test_round_two_gate_rejects_unrelated_rewrite_and_new_negation():
    item = {
        "english": "Changli deals very high damage",
        "translated": "查格丽的伤害非常高",
        "reasons": ["term_mismatch"],
        "term_hints": {"Changli": "长离"},
        "start": "00:00:00,000",
        "end": "00:00:03,000",
    }
    unrelated = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "长离今天准备去买一杯奶茶",
            "confidence": 0.99,
            "reason": "修正人名",
        },
        {"Changli": "长离"},
    )
    introduced_negative = long_video._evaluate_round_two_decision(
        item,
        {
            "decision": "replace",
            "text": "长离的伤害并不高",
            "confidence": 0.99,
            "reason": "修正人名",
        },
        {"Changli": "长离"},
    )

    assert unrelated["accepted"] is False
    assert "excessive_rewrite" in unrelated["validation_reasons"]
    assert introduced_negative["accepted"] is False
    assert "introduced_negation" in introduced_negative["validation_reasons"]


def test_local_whisper_batches_only_audio_dependent_windows(tmp_path, monkeypatch):
    risk_path = tmp_path / "risk.generated.json"
    results_path = tmp_path / "round2.results.json"
    save_generated_risks(risk_path, [
        {
            "key": "audio", "start": "00:00:02,000", "end": "00:00:03,000",
            "needs_audio_evidence": True,
        },
        {
            "key": "text-only", "start": "00:00:10,000", "end": "00:00:11,000",
            "needs_audio_evidence": False,
        },
    ])
    save_round2_results(results_path, [])
    calls = []

    def fake_run(command, cancel_check=None):
        calls.append(command)
        windows_path = command[command.index("--windows-json") + 1]
        windows = json.loads(open(windows_path, encoding="utf-8").read())
        assert len(windows) == 1
        save_srt(windows[0]["output"], [
            Subtitle(1, "00:00:00,500", "00:00:01,500", "audio evidence"),
        ])
        return 0

    monkeypatch.setattr(long_video, "_run_cancellable", fake_run)
    long_video._collect_local_evidence(
        str(tmp_path / "video.mp4"),
        str(risk_path),
        str(results_path),
        str(tmp_path / "evidence"),
        "medium",
    )

    assert len(calls) == 1
    assert "--windows-json" in calls[0]
    items = {
        item["key"]: item
        for item in load_round2_results(str(results_path))["items"]
    }
    assert items["audio"]["local_evidence_text"] == "audio evidence"
    assert "text-only" not in items


def test_local_whisper_uses_the_declared_japanese_language(tmp_path, monkeypatch):
    risk_path = tmp_path / "risk.generated.json"
    results_path = tmp_path / "round2.results.json"
    save_generated_risks(risk_path, [{
        "key": "audio", "start": "00:00:02,000", "end": "00:00:03,000",
        "needs_audio_evidence": True,
        "source_language": "ja",
    }])
    save_round2_results(results_path, [])
    calls = []

    def fake_run(command, cancel_check=None):
        calls.append(command)
        windows_path = command[command.index("--windows-json") + 1]
        windows = json.loads(open(windows_path, encoding="utf-8").read())
        save_srt(windows[0]["output"], [
            Subtitle(1, "00:00:00,500", "00:00:01,500", "日本語の証拠"),
        ])
        return 0

    monkeypatch.setattr(long_video, "_run_cancellable", fake_run)
    long_video._collect_local_evidence(
        str(tmp_path / "video.mp4"),
        str(risk_path),
        str(results_path),
        str(tmp_path / "evidence"),
        "medium",
        source_language="ja",
    )

    assert calls[0][calls[0].index("--language") + 1] == "ja"


def test_whisper_child_output_is_forwarded_to_web_events():
    messages = []

    status = long_video._run_cancellable(
        [sys.executable, "-c", "print('Whisper loading complete')"],
        emit=messages.append,
    )

    assert status == 0
    assert messages == ["Whisper: Whisper loading complete"]


def test_pipeline_metrics_counts_untranslated_source_cues(tmp_path):
    """1c：成片中"无中文、非纯 SFX"的 cue 数进 quality.untranslated_source_cues。

    dry-run 的 [待翻译] 占位符含中文不计数；[laughter] 类 SFX 不计数；
    'dancing Holy crap' 这类真实漏译整句必须被看见。
    """
    final = tmp_path / "final.zh.srt"
    save_srt(str(final), [
        Subtitle(1, "00:00:01,000", "00:00:02,000", "dancing Holy crap"),
        Subtitle(2, "00:00:02,000", "00:00:03,000", "[laughter]"),
        Subtitle(3, "00:00:03,000", "00:00:04,000", "这是中文句子"),
        Subtitle(4, "00:00:04,000", "00:00:05,000", "[待翻译] hello there"),
        Subtitle(5, "00:00:05,000", "00:00:06,000", "wait，this is peak"),
    ])
    paths = {
        "round1_manifest": str(tmp_path / "r1.manifest.json"),
        "round2_manifest": str(tmp_path / "r2.manifest.json"),
        "final": str(final),
        "metrics": str(tmp_path / "metrics.json"),
    }

    payload = long_video._write_pipeline_metrics(
        paths, candidates=[], risks=[], primary_count=0, secondary_count=0,
    )

    quality = payload["quality"]
    assert quality["untranslated_source_cues"] == 2
    stored = json.loads(
        (tmp_path / "metrics.json").read_text(encoding="utf-8")
    )
    assert stored["quality"]["untranslated_source_cues"] == 2
