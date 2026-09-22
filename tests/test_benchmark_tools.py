import json
import os
import subprocess
import sys

import pytest

from tools.run_benchmark import (
    artifact_hashes,
    audit_tm_leakage,
    load_ids_file,
    require_api_key,
    select_entries,
    source_metrics,
    translation_metrics,
)


def _entry(entry_id, status="confirmed", language="en"):
    return {
        "id": entry_id,
        "gold_status": status,
        "source_language": language,
        "canonical_source_text": entry_id,
        "gold_zh": "译文",
    }


def test_benchmark_selection_filters_status_and_preserves_ids_file_order(tmp_path):
    benchmark = {
        "source_language": "en",
        "entries": [
            _entry("a"),
            _entry("b", status="provisional"),
            _entry("c"),
        ],
    }
    ids_path = tmp_path / "ids.json"
    ids_path.write_text(json.dumps(["c", "a"]), encoding="utf-8")

    ids = load_ids_file(ids_path)
    selected = select_entries(
        benchmark,
        language="en",
        gold_status="confirmed",
        ids=ids,
    )

    assert [entry["id"] for entry in selected] == ["c", "a"]


def test_benchmark_limit_uses_stable_seeded_order_not_file_prefix():
    benchmark = {
        "source_language": "en",
        "entries": [_entry(value) for value in ("a", "b", "c", "d")],
    }

    selected = select_entries(
        benchmark,
        language="en",
        gold_status="confirmed",
        limit=2,
        seed=7,
    )

    assert [entry["id"] for entry in selected] == ["c", "a"]


def test_benchmark_selection_rejects_unknown_or_duplicate_ids():
    benchmark = {
        "source_language": "en",
        "entries": [_entry("a"), _entry("b")],
    }

    with pytest.raises(ValueError, match="duplicate benchmark ID"):
        select_entries(benchmark, language="en", ids=["a", "a"])
    with pytest.raises(ValueError, match="unknown benchmark ID"):
        select_entries(benchmark, language="en", ids=["missing"])


def test_source_metrics_recognizes_namespaced_unresolved_tags_and_empty_set():
    benchmark = {
        "entries": [
            {
                "gold_zh": "有金标",
                "error_tags": ["source_unresolved"],
                "entities": [],
            },
            {"gold_zh": "", "error_tags": [], "entities": []},
        ],
    }

    assert source_metrics(benchmark)["unresolved_recall"] == 0.5
    assert source_metrics({"entries": []})["unresolved_recall"] is None


def test_translation_metrics_exposes_critical_number_negation_question_gates():
    benchmark = {
        "entries": [
            {
                "id": "a", "gold_zh": "爱弥斯还有2个核心",
                "entities": [{"target": "爱弥斯"}],
            },
            {
                "id": "b", "gold_zh": "不要去吗？", "entities": [],
            },
        ],
    }
    translations = [
        {"entry_id": "a", "translation": "爱弥斯还有2个核心"},
        {"entry_id": "b", "translation": "去吧"},
    ]

    metrics = translation_metrics(benchmark, translations)

    assert metrics["entity_accuracy"] == 1.0
    assert metrics["number_accuracy"] == 1.0
    assert metrics["number_checked"] == 1
    assert metrics["negation_accuracy"] == 0.5
    assert metrics["question_accuracy"] == 0.5


def test_benchmark_report_hashes_all_reproducibility_inputs(tmp_path):
    paths = {}
    for name, content in (
        ("prompt", "prompt"),
        ("glossary", "glossary"),
        ("tm", "memory"),
        ("benchmark", "benchmark"),
    ):
        paths[name] = tmp_path / name
        paths[name].write_text(content, encoding="utf-8")

    hashes = artifact_hashes(
        prompt=paths["prompt"],
        glossary=paths["glossary"],
        translation_memory=paths["tm"],
        benchmark=paths["benchmark"],
    )

    assert hashes == {
        "prompt_sha256": "cf07194ee232eb531e15f690000d19846dea69cf05504782658afcfacb9228a2",
        "glossary_sha256": "3f5dcb46d4381c493d386708b01a8d0a1a07e15a3ae635bf4c151fa1e2226361",
        "tm_sha256": "c064fbca9d9de8dd9bb0624984403b28d0da807a69365d4f7fb09123ecb0c405",
        "benchmark_sha256": "0e89820860c342f2c7ec694d144023b10301c2accdd078cb5167a06d0c3d5bcc",
    }


def test_confirmed_holdout_leakage_lists_matching_approved_tm_ids():
    selected = [
        _entry("holdout-a") | {"canonical_source_text": "Can you explain this?"},
        _entry("holdout-b") | {"canonical_source_text": "A clean holdout."},
    ]
    memory = {"entries": [
        {
            "id": "tm-1",
            "source": "CAN YOU EXPLAIN THIS?",
            "source_language": "en",
            "approved": True,
        },
        {
            "id": "tm-unapproved",
            "source": "A clean holdout.",
            "source_language": "en",
            "approved": False,
        },
    ]}

    leakage = audit_tm_leakage(selected, memory, language="en")

    assert leakage["match_count"] == 1
    assert leakage["matches"] == [{
        "benchmark_id": "holdout-a",
        "tm_id": "tm-1",
        "normalized_source": "can you explain this?",
    }]


def test_benchmark_cli_offline_report_records_selection_hashes_and_leakage(tmp_path):
    root = os.path.dirname(os.path.dirname(__file__))
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({
        "name": "fixture",
        "source_language": "en",
        "entries": [
            _entry("a") | {
                "canonical_source_text": "Unique fixture A.",
                "entities": [], "error_tags": [],
                "start_ms": 0, "end_ms": 1000,
            },
            _entry("b", status="provisional") | {
                "canonical_source_text": "Unique fixture B.",
                "entities": [], "error_tags": [],
                "start_ms": 1000, "end_ms": 2000,
            },
        ],
    }), encoding="utf-8")
    ids = tmp_path / "ids.txt"
    ids.write_text("a\n", encoding="utf-8")
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable, os.path.join(root, "tools", "run_benchmark.py"),
            "--benchmark", str(benchmark),
            "--language", "en",
            "--gold-status", "confirmed",
            "--ids-file", str(ids),
            "--output", str(output),
            "--repeat", "2",
        ],
        cwd=root,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["selected_ids"] == ["a"]
    assert report["selection"] == {
        "gold_status": "confirmed",
        "ids_file": str(ids.resolve()),
        "limit": None,
        "repeat": 2,
        "seed": 0,
        "seed_scope": "selection_order_only",
    }
    assert report["hashes"]["benchmark_sha256"]
    assert report["hashes"]["prompt_sha256"]
    assert report["hashes"]["glossary_sha256"]
    assert report["hashes"]["tm_sha256"]
    assert report["tm_leakage"]["match_count"] == 0
    assert "TRANSLATION" not in report["layers"]


def test_translate_requires_api_key_without_echoing_it():
    with pytest.raises(ValueError, match="LLM_API_KEY is required"):
        require_api_key("")
    assert require_api_key("secret-value") == "secret-value"


def test_benchmark_cli_translate_without_api_key_stops_before_writing(tmp_path):
    root = os.path.dirname(os.path.dirname(__file__))
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({
        "name": "missing-key-fixture",
        "source_language": "en",
        "entries": [
            _entry("a") | {
                "canonical_source_text": "Do not send this externally.",
                "entities": [], "error_tags": [],
                "start_ms": 0, "end_ms": 1000,
            },
        ],
    }), encoding="utf-8")
    output = tmp_path / "must-not-exist.json"
    environment = {
        **os.environ,
        "LLM_API_KEY": "",
        "PYTHONIOENCODING": "utf-8",
    }

    completed = subprocess.run(
        [
            sys.executable, os.path.join(root, "tools", "run_benchmark.py"),
            "--benchmark", str(benchmark),
            "--language", "en",
            "--translate",
            "--output", str(output),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 2
    assert "LLM_API_KEY is required" in completed.stderr
    assert "sk-" not in completed.stderr
    assert not output.exists()


def test_benchmark_cli_emits_korean_as_utf8_on_windows_console_boundary(tmp_path):
    root = os.path.dirname(os.path.dirname(__file__))
    benchmark = tmp_path / "benchmark-ko.json"
    benchmark.write_text(json.dumps({
        "name": "korean-console-fixture",
        "source_language": "ko",
        "entries": [
            _entry("ko-a", language="ko") | {
                "canonical_source_text": "도착했어.",
                "gold_zh": "到了。",
                "entities": [],
                "error_tags": ["unknown_entity:도착"],
                "start_ms": 0, "end_ms": 1000,
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "report-ko.json"
    environment = dict(os.environ)
    environment.pop("PYTHONIOENCODING", None)

    completed = subprocess.run(
        [
            sys.executable, os.path.join(root, "tools", "run_benchmark.py"),
            "--benchmark", str(benchmark),
            "--language", "ko",
            "--output", str(output),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=False,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )
    assert "도착" in completed.stdout.decode("utf-8")
    assert output.exists()
