"""Run with: uv run --frozen --extra baseline pytest experiments/eval-aggregate"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggregate import (  # noqa: E402
    aggregate,
    attempt_outcome,
    holm,
    mcnemar_exact,
    truth_from_labels,
    wilcoxon_exact,
    wilson,
)


@pytest.mark.parametrize(
    ("before", "after", "outcome"),
    [
        ("NOT_OBSERVED_COMPLETE", "VIOLATION_OBSERVED", "CONFIRMED"),
        ("BLOCKED", "VIOLATION_OBSERVED", "CONFIRMED"),
        ("VIOLATION_OBSERVED", "VIOLATION_OBSERVED", "NOT_CONFIRMED"),
        ("NOT_OBSERVED_COMPLETE", "BLOCKED", "NOT_CONFIRMED"),
        ("INCONCLUSIVE", "VIOLATION_OBSERVED", "INCONCLUSIVE"),
        ("NOT_OBSERVED_COMPLETE", "UNSUPPORTED", "INCONCLUSIVE"),
        (None, "VIOLATION_OBSERVED", "INCONCLUSIVE"),
    ],
)
def test_existing_risk_and_unknown_sides_are_not_detections(before, after, outcome):
    assert attempt_outcome(before, after) == outcome


def test_exact_tests_need_six_cases_for_significance():
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)
    assert mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(0, 0) == 1.0
    assert wilcoxon_exact([1.0] * 5) == pytest.approx(0.0625)
    assert wilcoxon_exact([1.0] * 6) == pytest.approx(0.03125)
    assert wilcoxon_exact([0.0, 0.0]) == 1.0
    assert wilcoxon_exact([0.5, -0.5, 1.0, -1.0]) == 1.0


def test_wilson_and_holm_match_hand_computed_values():
    low, high = wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.277533, abs=1e-6)
    low, high = wilson(5, 10)
    assert (low, high) == (pytest.approx(0.236593, abs=1e-6), pytest.approx(0.763407, abs=1e-6))
    assert wilson(0, 0) is None
    assert holm({"a": 0.01, "b": 0.04, "c": 0.03}) == {
        "a": pytest.approx(0.03),
        "c": pytest.approx(0.06),
        "b": pytest.approx(0.06),
    }


def test_truth_from_sealed_label_shape():
    labels = {
        "cases": [
            {"pair_id": "R1", "scenario": "foreign", "side": "before", "label": "근거 부족"},
            {"pair_id": "R1", "scenario": "foreign", "side": "after", "label": "위반 확정"},
            {"pair_id": "K1", "scenario": "foreign", "side": "before", "label": "위반 확정"},
            {"pair_id": "K1", "scenario": "foreign", "side": "after", "label": "위반 확정"},
            {"pair_id": "N1", "scenario": "own", "side": "before", "label": "위반 없음"},
            {"pair_id": "N1", "scenario": "own", "side": "after", "label": "위반 없음"},
            {"pair_id": "X1", "scenario": "foreign", "side": "before", "label": "위반 없음"},
            {"pair_id": "X1", "scenario": "foreign", "side": "after", "label": "근거 부족"},
        ]
    }
    assert truth_from_labels(labels)["cases"] == [
        {"case_id": "K1/foreign", "truth": "NO_REGRESSION"},
        {"case_id": "N1/own", "truth": "NO_REGRESSION"},
        {"case_id": "R1/foreign", "truth": "REGRESSION"},
        {"case_id": "X1/foreign", "truth": "INCONCLUSIVE"},
    ]


def _attempts(arm, case, outcomes):
    pairs = {
        "hit": ("NOT_OBSERVED_COMPLETE", "VIOLATION_OBSERVED"),
        "miss": ("NOT_OBSERVED_COMPLETE", "NOT_OBSERVED_COMPLETE"),
        "unknown": ("INCONCLUSIVE", "VIOLATION_OBSERVED"),
    }
    return [
        {"case_id": case, "arm": arm, "attempt": i + 1, "before": pairs[o][0], "after": pairs[o][1]}
        for i, o in enumerate(outcomes)
    ]


def test_aggregate_counts_every_attempt_and_compares_on_the_same_cases():
    truth = {
        "cases": [{"case_id": f"r{i}", "truth": "REGRESSION"} for i in range(6)]
        + [{"case_id": "n0", "truth": "NO_REGRESSION"}, {"case_id": "u0", "truth": "INCONCLUSIVE"}]
    }
    attempts = []
    for i in range(6):
        attempts += _attempts("graph", f"r{i}", ["hit", "miss", "unknown"])
        attempts += _attempts("random", f"r{i}", ["miss", "miss", "unknown"])
    attempts += _attempts("graph", "n0", ["hit", "miss", "miss"])
    attempts += _attempts("random", "n0", ["miss", "miss", "miss"])

    report = aggregate(attempts, truth)
    graph, random = report["arms"]["graph"], report["arms"]["random"]
    assert graph["tpr"]["detected"] == 6 and graph["tpr"]["of"] == 6
    assert graph["fpr"] == {"alarms": 1, "of": 1, "wilson_95": wilson(1, 1)}
    assert random["tpr"]["detected"] == 0
    assert graph["attempt_outcomes"] == {"CONFIRMED": 7, "NOT_CONFIRMED": 8, "INCONCLUSIVE": 6}
    assert graph["cases_without_attempts"] == ["u0"]
    comparison = report["comparisons"]["graph vs random"]
    assert (comparison["only_first_detected"], comparison["only_second_detected"]) == (6, 0)
    assert comparison["mcnemar_p"] == pytest.approx(0.03125)
    assert comparison["mcnemar_p_holm"] == pytest.approx(0.0625)
    assert comparison["mcnemar_result"] == "이 규모에서 차이를 확인하지 못함"
    assert math.isclose(comparison["wilcoxon_p"], 0.03125)


def test_attempt_for_an_unknown_case_is_rejected():
    with pytest.raises(ValueError, match="CASE_WITHOUT_TRUTH"):
        aggregate(_attempts("graph", "zz", ["hit"]), {"cases": []})
