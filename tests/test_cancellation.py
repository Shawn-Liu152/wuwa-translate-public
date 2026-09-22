import json
import logging
import threading
import time

import pytest

from pipeline import long_video, main
from pipeline.pipeline_manifest import PipelineCancelled
from pipeline.translate.llm import TranslateResult
from pipeline.translate.llm import LLMAuthenticationError
from pipeline.parser.srt_parser import Subtitle, parse_srt
from pipeline.preprocess.music import strip_music_annotations


class _Glossary:
    def extract_from_text(self, text, game="wuwa"):
        return {}

    def list_all(self, game="wuwa"):
        return []


def test_round_one_batches_adapt_between_24_and_32_items():
    subtitles = [
        Subtitle(
            id=index,
            start="00:00:00,000",
            end="00:00:01,000",
            text="short line",
        )
        for index in range(1, 71)
    ]

    batches = main.build_adaptive_batches(
        subtitles, target_size=28, token_budget=100_000
    )

    assert [len(batch) for batch in batches] == [32, 32, 6]
    assert [item.id for batch in batches for item in batch] == list(range(1, 71))


def test_round_one_token_budget_splits_long_batches_at_hard_limit():
    subtitles = [
        Subtitle(
            id=index,
            start="00:00:00,000",
            end="00:00:01,000",
            text="x" * 400,
        )
        for index in range(1, 51)
    ]

    batches = main.build_adaptive_batches(
        subtitles, target_size=28, token_budget=2_600
    )

    assert [len(batch) for batch in batches] == [24, 24, 2]

    very_long = [
        Subtitle(
            id=index,
            start="00:00:00,000",
            end="00:00:01,000",
            text="x" * 4_000,
        )
        for index in range(1, 6)
    ]
    assert [len(batch) for batch in main.build_adaptive_batches(
        very_long, target_size=28, token_budget=2_600
    )] == [2, 2, 1]


def test_round_one_batching_keeps_one_fragmented_sentence_together():
    subtitles = [
        Subtitle(101, "00:00:00,000", "00:00:01,000", "I don't even"),
        Subtitle(102, "00:00:01,000", "00:00:02,000", "know what she"),
        Subtitle(
            103, "00:00:02,000", "00:00:03,000",
            "was trying to do there.",
        ),
        Subtitle(104, "00:00:04,000", "00:00:05,000", "Next thought."),
    ]

    batches = main.build_adaptive_batches(
        subtitles, target_size=2, token_budget=10_000
    )

    assert [[item.id for item in batch] for batch in batches] == [
        [101, 102, 103], [104],
    ]


def _minimal_pipeline(monkeypatch):
    monkeypatch.setattr(main, "load_glossary_db", lambda data_dir=None: _Glossary())
    monkeypatch.setattr(main, "load_prompt_builder", lambda data_dir=None: object())
    # resume 的 context_signature 依赖 approved translation memory；
    # 真实 data/translation_memory.json 会随种子库增长而变化，测试必须隔离。
    # main.py 在函数内 `from pipeline.translation_memory import TranslationMemoryStore`，
    # 必须 patch 源头模块（pipeline.translation_memory），而非 main 上的名字。
    import pipeline.translation_memory as tm_module
    monkeypatch.setattr(
        tm_module, "TranslationMemoryStore",
        lambda path: type("_EmptyMemoryStore", (), {"list_all": lambda self: []})(),
    )
    monkeypatch.setattr(main.Config, "ENABLE_ALIAS", False)
    monkeypatch.setattr(main.Config, "ENABLE_JAPANESE_FILTER", False)
    monkeypatch.setattr(main.Config, "ENABLE_MUSIC_DETECTION", False)
    monkeypatch.setattr(main.Config, "ENABLE_VALIDATION", False)
    monkeypatch.setattr(main.Config, "ENABLE_PROFILER", False)


def test_round_one_cancellation_is_not_batch_failure(tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    _minimal_pipeline(monkeypatch)

    class Translator:
        def translate(self, batch, **kwargs):
            return [TranslateResult(id=item.id, original=item.text, translated="你好") for item in batch]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())
    checks = iter([False, False, True])

    with pytest.raises(PipelineCancelled):
        main.run_pipeline(
            str(source), str(output), model="test", api_key="key", batch_size=1,
            manifest_path=str(manifest), dedupe=False,
            cancel_check=lambda: next(checks, True),
        )

    assert not output.exists()
    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert payload["batches"]["0"]["state"] == "pending"


def test_round_one_retries_only_ids_with_untranslated_proper_nouns(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nShe sounds like Violet\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            translated = "她听起来像 Violet" if len(calls) == 1 else "她听起来像薇尔莉特"
            return [
                TranslateResult(
                    id=item.id, original=item.text, translated=translated,
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key",
        batch_size=1, dedupe=False,
    ) == 0
    manifest = json.loads(
        (tmp_path / "output.srt.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["options"]["provider"] == {
        "name": "unknown",
        "hostname": "",
    }
    assert calls == [[1], [1]]
    assert "薇尔莉特" in output.read_text(encoding="utf-8-sig")


def test_round_one_retries_only_ids_missing_required_glossary_translation(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSuisui is here\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)

    class SuisuiGlossary:
        def extract_from_text(self, text, game="wuwa"):
            return {"Suisui": "穗穗"} if "Suisui" in text else {}

        def list_all(self, game="wuwa"):
            return [{"english": "Suisui", "chinese": "穗穗"}]

    monkeypatch.setattr(
        main, "load_glossary_db", lambda data_dir=None: SuisuiGlossary()
    )
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            translated = "苏伊来了" if len(calls) == 1 else "穗穗来了"
            return [
                TranslateResult(
                    id=item.id, original=item.text, translated=translated,
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key",
        batch_size=1, dedupe=False,
    ) == 0
    assert calls == [[1], [1]]
    assert "穗穗来了" in output.read_text(encoding="utf-8-sig")


def test_round_one_does_not_retry_a_soft_gameplay_word_in_normal_speech(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nLaunch the game now\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)

    class GameplayGlossary:
        def extract_from_text(self, text, game="wuwa"):
            return {"Launch": "击飞"} if "Launch" in text else {}

        def list_all(self, game="wuwa", source_language=None):
            return [{
                "english": "Launch",
                "chinese": "击飞",
                "source_term": "Launch",
                "target_term": "击飞",
                "category": "combat",
            }]

    monkeypatch.setattr(
        main, "load_glossary_db", lambda data_dir=None: GameplayGlossary()
    )
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            return [
                TranslateResult(
                    id=item.id,
                    original=item.text,
                    translated="现在启动游戏",
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key",
        batch_size=1, dedupe=False,
    ) == 0
    assert calls == [[1]]
    assert "现在启动游戏" in output.read_text(encoding="utf-8-sig")


def test_round_one_does_not_force_context_sensitive_weapon_or_gacha_words(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nShe is flying on a sword\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nI saw that banner yesterday\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)

    class ContextSensitiveGlossary:
        def extract_from_text(self, text, game="wuwa"):
            matched = {}
            if "sword" in text.casefold():
                matched["Sword"] = "迅刀"
            if "banner" in text.casefold():
                matched["Banner"] = "卡池"
            return matched

        def list_all(self, game="wuwa", source_language=None):
            return [
                {
                    "english": "Sword", "chinese": "迅刀",
                    "source_term": "Sword", "target_term": "迅刀",
                    "category": "weapon",
                },
                {
                    "english": "Banner", "chinese": "卡池",
                    "source_term": "Banner", "target_term": "卡池",
                    "category": "gacha",
                },
            ]

    monkeypatch.setattr(
        main, "load_glossary_db", lambda data_dir=None: ContextSensitiveGlossary()
    )
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            translations = {
                1: "她踩着剑飞",
                2: "我昨天看到了那张横幅",
            }
            return [
                TranslateResult(
                    id=item.id,
                    original=item.text,
                    translated=translations[item.id],
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key",
        batch_size=2, dedupe=False,
    ) == 0
    assert calls == [[1, 2]]
    rendered = output.read_text(encoding="utf-8-sig")
    assert "她踩着剑飞" in rendered
    assert "我昨天看到了那张横幅" in rendered


def test_round_one_retries_whole_batch_after_partial_response(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nFirst source\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nSecond source\n\n"
        "3\n00:00:04,000 --> 00:00:05,000\nThird source\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            if len(calls) == 1:
                # A real provider response omitted #3 and shifted the next
                # source's translation under earlier IDs.  The non-empty
                # partial rows are therefore not trustworthy.
                return [
                    TranslateResult(1, batch[0].text, "错误：这是第二条"),
                    TranslateResult(2, batch[1].text, "错误：这是第三条"),
                    TranslateResult(3, batch[2].text, ""),
                ]
            translations = {1: "第一条", 2: "第二条", 3: "第三条"}
            return [
                TranslateResult(
                    item.id, item.text, translations[item.id],
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key",
        batch_size=3, dedupe=False,
    ) == 0
    assert calls == [[1, 2, 3], [1, 2, 3]]
    rendered = output.read_text(encoding="utf-8-sig")
    assert "错误" not in rendered
    assert all(text in rendered for text in ("第一条", "第二条", "第三条"))


def test_round_two_cancels_after_model_returns(monkeypatch):
    calls = []

    class Translator:
        def _call_api(self, prompt):
            calls.append(prompt)
            return "[1] 修正", 0, 0

    monkeypatch.setattr(long_video, "_load_glossary", lambda game, data_dir=None: {})
    monkeypatch.setattr(long_video, "create_translator", lambda **kwargs: Translator())
    checks = iter([False, True])
    items = [{
        "subtitle_id": 1, "english": "Hello", "capcut_en": "Hello",
        "whisper_en": "Hello", "local_evidence_text": "Hello local",
        "translated": "你好", "reasons": ["test"],
    }]

    with pytest.raises(PipelineCancelled):
        long_video._round_two(
            items, "test", "key", "wuwa", cancel_check=lambda: next(checks, True)
        )
    assert len(calls) == 1
    assert "Hello local" in calls[0]


def test_minimal_glossary_adapter_preserves_conservative_required_terms():
    class MinimalGlossary:
        pass

    assert main._hard_glossary_targets(MinimalGlossary(), "wuwa", "ja") is None


def test_legacy_call_without_cancel_check_still_runs(tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    _minimal_pipeline(monkeypatch)

    progress = []
    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="", batch_size=1,
        dry_run=True, dedupe=False,
        progress_callback=lambda completed, total: progress.append((completed, total)),
    ) == 0
    assert output.exists()
    assert progress == [(0, 1), (1, 1)]


def test_authentication_failure_stops_after_initial_parallel_wave(tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nWorld\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append(batch)
            raise LLMAuthenticationError("API 401: invalid key", status_code=401)

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    with pytest.raises(LLMAuthenticationError):
        main.run_pipeline(
            str(source), str(output), model="test", api_key="bad", batch_size=1,
            manifest_path=str(manifest), dedupe=False,
        )

    # Stateful jobs intentionally allow a bounded parallel wave. A fatal
    # provider error cancels queued work, but already active calls may finish.
    assert 1 <= len(calls) <= main.Config.STATEFUL_MAX_CONCURRENT
    assert not output.exists()
    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["batches"]["0"]["state"] == "failed"
    assert len(payload["batches"]) == len(calls)
    assert all(
        batch["state"] == "failed"
        for batch in payload["batches"].values()
    )


def test_batch_failure_never_writes_partial_output_when_validation_is_disabled(tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    _minimal_pipeline(monkeypatch)

    class Translator:
        def translate(self, batch, **kwargs):
            raise ValueError("malformed provider response")

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    status = main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=1,
        manifest_path=str(manifest), dedupe=False,
    )

    assert status == 2
    assert not output.exists()
    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"


def test_round_one_failure_redacts_untrusted_error_from_manifest_and_logs(
        tmp_path, monkeypatch, caplog):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    malicious = (
        "Authorization: Bearer sk-live-round-one Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )

    class Translator:
        def translate(self, batch, **kwargs):
            raise RuntimeError(malicious)

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())
    caplog.set_level(logging.INFO)

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=1,
        manifest_path=str(manifest), dedupe=False,
    ) == 2

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    exposed = manifest.read_text(encoding="utf-8") + caplog.text
    for secret in (
        "sk-live-round-one",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
        str(tmp_path),
    ):
        assert secret not in exposed
    assert payload["input"]["path"] == "input.srt"
    assert payload["batches"]["0"]["error_code"] == "translation_batch_error"


def test_successful_manifest_records_output_hash(tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    _minimal_pipeline(monkeypatch)

    class Translator:
        def translate(self, batch, **kwargs):
            return [TranslateResult(id=item.id, original=item.text, translated="你好") for item in batch]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=1,
        manifest_path=str(manifest), dedupe=False,
    ) == 0

    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["artifacts"]["output"]["sha256"]
    assert payload["input"]["path"] == "input.srt"
    assert payload["artifacts"]["output"]["path"] == "output.srt"
    assert str(tmp_path) not in manifest.read_text(encoding="utf-8")


def test_stateful_round_one_uses_bounded_parallelism_and_valid_manifest(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text("\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},800\nLine {index}"
        for index in range(1, 7)
    ), encoding="utf-8")
    _minimal_pipeline(monkeypatch)
    monkeypatch.setattr(main.Config, "STATEFUL_MAX_CONCURRENT", 3)
    active = 0
    max_active = 0
    lock = threading.Lock()

    class Translator:
        def translate(self, batch, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return [
                TranslateResult(
                    id=item.id, original=item.text, translated=f"译文{item.id}",
                    tokens_in=10, tokens_out=2,
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=1,
        manifest_path=str(manifest), dedupe=False,
    ) == 0

    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert max_active >= 2
    assert payload["status"] == "completed"
    assert payload["metrics"]["parallelism"] == 3
    assert payload["metrics"]["tokens_in"] == 60
    assert all(
        batch["state"] == "success"
        for batch in payload["batches"].values()
    )


def test_missing_translation_discards_partial_response_and_retries_whole_batch(
        tmp_path, monkeypatch):
    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest = tmp_path / "manifest.json"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nWorld\n",
        encoding="utf-8",
    )
    _minimal_pipeline(monkeypatch)
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            return [
                TranslateResult(
                    id=item.id, original=item.text,
                    translated=("" if item.id == 2 and len(calls) == 1 else f"译文{item.id}"),
                )
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=2,
        manifest_path=str(manifest), dedupe=False,
    ) == 0

    assert calls == [[1, 2], [1, 2]]
    payload = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert payload["batches"]["0"]["state"] == "success"
    assert all(item["translated"] for item in payload["batches"]["0"]["results"])


def test_resume_repairs_only_empty_item_from_legacy_success_batch(
        tmp_path, monkeypatch):
    from pipeline.pipeline_manifest import create_manifest, save_manifest, set_batch

    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest_path = tmp_path / "manifest.json"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nWorld\n",
        encoding="utf-8",
    )
    options = {
        "model": "test", "batch_size": 2, "game": "wuwa",
        "term_anchor": True, "refine": False,
    }
    manifest = create_manifest(str(source), options, [])
    set_batch(manifest, "0", "success", ids=[1, 2], results=[
        {"id": 1, "original": "Hello", "translated": "你好",
         "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "cached": False},
        {"id": 2, "original": "World", "translated": "",
         "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "cached": False},
    ])
    save_manifest(str(manifest_path), manifest)
    _minimal_pipeline(monkeypatch)
    calls = []

    class Translator:
        def translate(self, batch, **kwargs):
            calls.append([item.id for item in batch])
            return [
                TranslateResult(id=item.id, original=item.text, translated="世界")
                for item in batch
            ]

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="key", batch_size=2,
        manifest_path=str(manifest_path), resume=True, dedupe=False,
    ) == 0

    assert calls == [[2]]
    repaired = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    translations = {
        item["id"]: item["translated"]
        for item in repaired["batches"]["0"]["results"]
    }
    assert translations == {1: "你好", 2: "世界"}


def test_resume_ignores_results_new_preprocessing_now_skips(
        tmp_path, monkeypatch):
    """A preprocessing upgrade must not invalidate an otherwise complete resume."""
    from pipeline.pipeline_manifest import create_manifest, save_manifest, set_batch

    source = tmp_path / "input.srt"
    output = tmp_path / "output.srt"
    manifest_path = tmp_path / "manifest.json"
    source.write_text("\n\n".join(
        f"{item_id}\n00:00:00,000 --> 00:00:01,000\n"
        + (">> [music]" if item_id == 16 else f"Line {item_id}")
        for item_id in range(1, 41)
    ), encoding="utf-8")
    options = {
        "model": "test", "batch_size": 28, "game": "wuwa",
        "term_anchor": True, "refine": False,
        "batching": "token-adaptive-semantic-v2",
        "context_strategy": "local-6-history-12-global-v2",
        "context_signature": __import__("hashlib").sha256(
            b'{"memory": [], "video": {}}'
        ).hexdigest(),
        "source_token_budget": main.Config.ROUND1_SOURCE_TOKEN_BUDGET,
    }
    manifest = create_manifest(str(source), options, [])
    def saved_result(item_id):
        return {
            "id": item_id,
            "original": (f"Line {item_id}\n>>" if item_id == 33
                         else f"Line {item_id}"),
            "translated": f"译文{item_id}", "tokens_in": 0,
            "tokens_out": 0, "cost": 0.0, "cached": False,
        }

    # The old first batch failed only because ID 16 was a music cue. ID 33
    # already succeeded in the next batch and must be reused after rebatching.
    set_batch(
        manifest, "0", "failed", ids=list(range(1, 33)),
        results=[saved_result(item_id) for item_id in range(1, 33)
                 if item_id != 16],
        error="模型连续漏译字幕: #16",
    )
    set_batch(
        manifest, "1", "success", ids=list(range(33, 41)),
        results=[saved_result(item_id) for item_id in range(33, 41)],
    )
    save_manifest(str(manifest_path), manifest)
    _minimal_pipeline(monkeypatch)
    monkeypatch.setattr(main.Config, "ENABLE_MUSIC_DETECTION", True)

    class Detector:
        @staticmethod
        def strip_markers(text):
            return strip_music_annotations(text)

    monkeypatch.setattr(main, "load_music_detector", lambda data_dir=None: Detector())

    class Translator:
        def preflight(self):
            raise AssertionError("complete manifest resume must not preflight the model")

        def translate(self, batch, **kwargs):
            raise AssertionError("complete manifest resume must not call the model")

    monkeypatch.setattr(main, "create_translator", lambda **kwargs: Translator())

    assert main.run_pipeline(
        str(source), str(output), model="test", api_key="", batch_size=28,
        manifest_path=str(manifest_path), resume=True, dedupe=False,
    ) == 0
    rendered = {item.id: item.text for item in parse_srt(str(output))}
    assert rendered.pop(16, "") == ""
    assert rendered == {
        item_id: f"译文{item_id}" for item_id in range(1, 41)
        if item_id != 16
    }
