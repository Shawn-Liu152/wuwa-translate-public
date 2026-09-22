# -*- coding: utf-8 -*-
"""Compare two paid benchmark reports under strict one-variable A/B rules."""
import argparse
import json
from pathlib import Path


HIGHER_IS_BETTER_CRITICAL = (
    "entity_accuracy",
    "number_accuracy",
    "negation_accuracy",
    "question_accuracy",
)
LOWER_IS_BETTER_CRITICAL = (
    "hangul_residue_units",
    "suspected_hallucinated_units",
)


def _translation_runs(report):
    translation = (report.get("layers") or {}).get("TRANSLATION")
    if not translation or not translation.get("runs"):
        raise ValueError("both reports must contain TRANSLATION runs")
    return translation["runs"]


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _aggregate(report):
    runs = _translation_runs(report)
    metric_names = sorted({
        name for run in runs for name in (run.get("metrics") or {})
    })
    metrics = {
        name: _mean([
            (run.get("metrics") or {}).get(name) for run in runs
        ])
        for name in metric_names
    }
    usage = [run.get("usage") or {} for run in runs]
    costs = [item.get("cost") for item in usage]
    cost_available = bool(costs) and all(cost is not None for cost in costs)
    return {
        "repeat": len(runs),
        "metrics": metrics,
        "tokens_in": sum(int(item.get("tokens_in") or 0) for item in usage),
        "tokens_out": sum(int(item.get("tokens_out") or 0) for item in usage),
        "tokens_total": sum(
            int(item.get("tokens_total") or (
                int(item.get("tokens_in") or 0)
                + int(item.get("tokens_out") or 0)
            ))
            for item in usage
        ),
        "wall_seconds": round(sum(
            float(item.get("wall_seconds") or 0) for item in usage
        ), 3),
        "cost": sum(float(cost) for cost in costs) if cost_available else None,
        "cost_availability": (
            "available" if cost_available else "unavailable"
        ),
    }


def _regression(metric, baseline, candidate, *, higher_is_better):
    if baseline is None and candidate is None:
        return None
    if baseline is not None and candidate is None:
        return {
            "metric": metric, "baseline": baseline, "candidate": candidate,
            "reason": "candidate metric became unobservable",
        }
    if baseline is None:
        return None
    regressed = (
        candidate < baseline if higher_is_better else candidate > baseline
    )
    if not regressed:
        return None
    return {
        "metric": metric,
        "baseline": baseline,
        "candidate": candidate,
        "delta": candidate - baseline,
    }


def compare_runs(baseline, candidate):
    if baseline.get("source_language") != candidate.get("source_language"):
        raise ValueError("source_language differs")
    if baseline.get("selected_ids") != candidate.get("selected_ids"):
        raise ValueError("selected_ids must be identical and ordered identically")
    for name in ("benchmark_sha256", "glossary_sha256", "tm_sha256"):
        if (baseline.get("hashes") or {}).get(name) != (
            candidate.get("hashes") or {}
        ).get(name):
            raise ValueError(f"fixed input hash differs: {name}")
    if (baseline.get("selection") or {}).get("repeat") != (
        candidate.get("selection") or {}
    ).get("repeat"):
        raise ValueError("repeat differs")

    variables = {
        "model": (baseline.get("model"), candidate.get("model")),
        "provider": (baseline.get("provider"), candidate.get("provider")),
        "prompt": (
            (baseline.get("hashes") or {}).get("prompt_sha256"),
            (candidate.get("hashes") or {}).get("prompt_sha256"),
        ),
    }
    changed = [name for name, values in variables.items() if values[0] != values[1]]
    if len(changed) != 1:
        raise ValueError(
            "exactly one variable must change among model, provider, and prompt"
        )

    baseline_aggregate = _aggregate(baseline)
    candidate_aggregate = _aggregate(candidate)
    baseline_metrics = baseline_aggregate["metrics"]
    candidate_metrics = candidate_aggregate["metrics"]
    critical_regressions = []
    for metric in HIGHER_IS_BETTER_CRITICAL:
        item = _regression(
            metric, baseline_metrics.get(metric), candidate_metrics.get(metric),
            higher_is_better=True,
        )
        if item:
            critical_regressions.append(item)
    for metric in LOWER_IS_BETTER_CRITICAL:
        item = _regression(
            metric, baseline_metrics.get(metric), candidate_metrics.get(metric),
            higher_is_better=False,
        )
        if item:
            critical_regressions.append(item)

    language = baseline["source_language"]
    return {
        "verdict": (
            "FAIL_CRITICAL_REGRESSION" if critical_regressions else "PASS"
        ),
        "changed_variable": changed[0],
        "changed_from": variables[changed[0]][0],
        "changed_to": variables[changed[0]][1],
        "fixed_inputs": {
            name: (baseline.get("hashes") or {}).get(name)
            for name in ("benchmark_sha256", "glossary_sha256", "tm_sha256")
        } | {
            "selected_ids": baseline["selected_ids"],
            "repeat": (baseline.get("selection") or {}).get("repeat"),
        },
        "critical_regressions": critical_regressions,
        "by_language": {
            language: {
                "baseline": baseline_aggregate,
                "candidate": candidate_aggregate,
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compare strict one-variable benchmark A/B reports."
    )
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    try:
        result = compare_runs(baseline, candidate)
    except ValueError as error:
        parser.error(str(error))
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["verdict"].startswith("FAIL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
