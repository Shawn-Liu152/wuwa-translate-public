import pytest

from tools.compare_benchmark_runs import compare_runs


def _report(*, model, prompt_hash, metrics, usage=None):
    return {
        "source_language": "en",
        "provider": {"name": "provider", "hostname": "example.invalid"},
        "model": model,
        "selected_ids": ["a", "b"],
        "selection": {"repeat": 1, "seed": 7},
        "hashes": {
            "benchmark_sha256": "benchmark",
            "glossary_sha256": "glossary",
            "tm_sha256": "tm",
            "prompt_sha256": prompt_hash,
        },
        "layers": {
            "TRANSLATION": {
                "repeat": 1,
                "runs": [{
                    "repeat_index": 0,
                    "metrics": metrics,
                    "usage": usage or {
                        "tokens_in": 100,
                        "tokens_out": 50,
                        "wall_seconds": 2.0,
                        "cost": None,
                        "cost_availability": "unavailable",
                    },
                }],
            },
        },
    }


def _metrics(**updates):
    value = {
        "gold_exact_match": 1,
        "entity_accuracy": 1.0,
        "number_accuracy": 1.0,
        "negation_accuracy": 1.0,
        "question_accuracy": 1.0,
        "hangul_residue_units": 0,
        "suspected_hallucinated_units": 0,
    }
    value.update(updates)
    return value


def test_compare_requires_fixed_ids_hashes_and_exactly_one_changed_variable():
    baseline = _report(model="base", prompt_hash="prompt", metrics=_metrics())
    candidate = _report(model="candidate", prompt_hash="prompt", metrics=_metrics())

    result = compare_runs(baseline, candidate)

    assert result["changed_variable"] == "model"
    assert result["verdict"] == "PASS"
    assert result["by_language"]["en"]["candidate"]["tokens_total"] == 150

    candidate["selected_ids"] = ["b", "a"]
    with pytest.raises(ValueError, match="selected_ids"):
        compare_runs(baseline, candidate)


def test_compare_rejects_confounded_model_and_prompt_change():
    baseline = _report(model="base", prompt_hash="prompt-a", metrics=_metrics())
    candidate = _report(
        model="candidate", prompt_hash="prompt-b", metrics=_metrics(),
    )

    with pytest.raises(ValueError, match="exactly one variable"):
        compare_runs(baseline, candidate)


@pytest.mark.parametrize(
    "field,candidate_value",
    [
        ("entity_accuracy", 0.9),
        ("number_accuracy", 0.9),
        ("negation_accuracy", 0.9),
        ("hangul_residue_units", 1),
        ("suspected_hallucinated_units", 1),
    ],
)
def test_compare_critical_regression_is_a_veto(field, candidate_value):
    baseline = _report(model="base", prompt_hash="prompt", metrics=_metrics())
    candidate = _report(
        model="candidate",
        prompt_hash="prompt",
        metrics=_metrics(**{field: candidate_value}),
    )

    result = compare_runs(baseline, candidate)

    assert result["verdict"] == "FAIL_CRITICAL_REGRESSION"
    assert any(item["metric"] == field for item in result["critical_regressions"])
