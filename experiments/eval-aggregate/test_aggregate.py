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


def _labels(*rows):
    return {
        "cases": [
            {"pair_id": pair, "scenario": scenario, "side": side, "label": label}
            for pair, scenario, side, label in rows
        ]
    }


def test_truth_from_sealed_label_shape():
    labels = _labels(
        ("R1", "foreign", "before", "위반 없음"),
        ("R1", "foreign", "after", "위반 확정"),
        ("K1", "foreign", "before", "위반 확정"),
        ("K1", "foreign", "after", "위반 확정"),
        ("N1", "own", "before", "위반 없음"),
        ("N1", "own", "after", "위반 없음"),
        ("X1", "foreign", "before", "위반 없음"),
        ("X1", "foreign", "after", "근거 부족"),
    )
    assert truth_from_labels(labels)["cases"] == [
        {"case_id": "K1/foreign", "truth": "NO_REGRESSION"},
        {"case_id": "N1/own", "truth": "NO_REGRESSION"},
        {"case_id": "R1/foreign", "truth": "REGRESSION"},
        {"case_id": "X1/foreign", "truth": "INCONCLUSIVE"},
    ]


def test_unknown_before_side_cannot_make_a_confirmed_regression():
    # platform #128: a violation after an unknown before side may be an existing risk.
    labels = _labels(
        ("U1", "foreign", "before", "근거 부족"),
        ("U1", "foreign", "after", "위반 확정"),
        ("U2", "foreign", "before", "근거 부족"),
        ("U2", "foreign", "after", "위반 없음"),
    )
    assert truth_from_labels(labels)["cases"] == [
        {"case_id": "U1/foreign", "truth": "INCONCLUSIVE"},
        {"case_id": "U2/foreign", "truth": "NO_REGRESSION"},
    ]


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([("M1", "foreign", "before", "위반 없음")], "LABEL_MISSING"),
        ([("M1", "foreign", "after", "위반 확정")], "LABEL_MISSING"),
        (
            [
                ("D1", "foreign", "before", "위반 없음"),
                ("D1", "foreign", "after", "위반 확정"),
                ("D1", "foreign", "after", "위반 없음"),
            ],
            "DUPLICATE_LABEL",
        ),
        (
            [("T1", "foreign", "before", "위반 없음"), ("T1", "foreign", "after", "위반")],
            "UNKNOWN_LABEL",
        ),
    ],
)
def test_incomplete_or_ambiguous_label_sets_are_rejected(rows, error):
    with pytest.raises(ValueError, match=error):
        truth_from_labels(_labels(*rows))


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
    assert graph["attempt_outcomes"] == {
        "CONFIRMED": 7,
        "NOT_CONFIRMED": 8,
        "INCONCLUSIVE": 6,
        "NOT_SELECTED": 0,
        "NOT_EXECUTED": 0,
        "UNSUPPORTED": 0,
    }
    assert graph["cases_without_attempts"] == ["u0"]
    comparison = report["comparisons"]["graph vs random"]
    assert (comparison["only_first_detected"], comparison["only_second_detected"]) == (6, 0)
    assert comparison["regression_units"] == 6
    assert comparison["budget_matched"] is True
    assert comparison["mcnemar_p"] == pytest.approx(0.03125)
    assert comparison["mcnemar_p_holm"] == pytest.approx(0.0625)
    assert comparison["mcnemar_result"] == "이 규모에서 차이를 확인하지 못함"
    assert math.isclose(comparison["wilcoxon_p"], 0.03125)


def test_attempt_for_an_unknown_case_is_rejected():
    with pytest.raises(ValueError, match="CASE_WITHOUT_TRUTH"):
        aggregate(_attempts("graph", "zz", ["hit"]), {"cases": []})


def _truth(**cases):
    return {"cases": [{"case_id": c, "truth": t} for c, t in cases.items()]}


def _row(case, arm, attempt, status, before=None, after=None, runs=()):
    return {
        "case_id": case,
        "arm": arm,
        "attempt": attempt,
        "status": status,
        "before": before,
        "after": after,
        "runs": [{"side": side, "run_id": run_id} for side, run_id in runs],
    }


HIT = ("NOT_OBSERVED_COMPLETE", "VIOLATION_OBSERVED")


def test_repeated_attempt_number_is_rejected():
    # platform #128: a second record for the same attempt would count one try twice.
    attempts = _attempts("graph", "r0", ["hit"]) + _attempts("graph", "r0", ["miss"])
    with pytest.raises(ValueError, match="DUPLICATE_ATTEMPT"):
        aggregate(attempts, _truth(r0="REGRESSION"))


def test_one_run_reused_by_two_records_is_rejected():
    # docs #67: a reused raw observation would be counted as an independent try.
    attempts = [
        _row("p1/a", "graph", 1, "COMPLETE", *HIT, runs=[("before", "r-1"), ("after", "r-2")]),
        _row("p1/a", "graph", 2, "COMPLETE", *HIT, runs=[("before", "r-3"), ("after", "r-2")]),
    ]
    with pytest.raises(ValueError, match="SHARED_OBSERVATION"):
        aggregate(attempts, _truth(**{"p1/a": "REGRESSION"}))


def test_record_status_separates_not_selected_not_executed_and_unsupported():
    truth = _truth(
        **{
            "p1/a": "REGRESSION",
            "p1/b": "REGRESSION",
            "p1/c": "REGRESSION",
            "p1/d": "REGRESSION",
            "p1/e": "REGRESSION",
        }
    )
    attempts = [
        _row("p1/a", "rag", 1, "COMPLETE", *HIT),
        _row("p1/b", "rag", 1, "INCOMPLETE", "INCONCLUSIVE", "VIOLATION_OBSERVED"),
        _row("p1/c", "rag", 1, "NOT_SELECTED"),
        _row("p1/d", "rag", 1, "FAILED"),
        _row("p1/e", "rag", 1, "UNSUPPORTED"),
    ]
    arm = aggregate(attempts, truth)["arms"]["rag"]
    assert arm["attempt_outcomes"] == {
        "CONFIRMED": 1,
        "NOT_CONFIRMED": 0,
        "INCONCLUSIVE": 1,
        "NOT_SELECTED": 1,
        "NOT_EXECUTED": 1,
        "UNSUPPORTED": 1,
    }
    assert arm["record_statuses"] == {
        "COMPLETE": 1,
        "FAILED": 1,
        "INCOMPLETE": 1,
        "NOT_SELECTED": 1,
        "UNSUPPORTED": 1,
    }
    assert arm["tpr"]["detected"] == 1 and arm["tpr"]["of"] == 5


def test_arm_that_is_entirely_unsupported_is_not_compared():
    truth = _truth(**{"p1/a": "REGRESSION"})
    attempts = [
        _row("p1/a", "fixed", 1, "COMPLETE", *HIT),
        _row("p1/a", "random", 1, "UNSUPPORTED"),
    ]
    report = aggregate(attempts, truth)
    assert report["unsupported_arms"] == ["random"]
    assert list(report["arms"]) == ["fixed"]
    assert report["comparisons"] == {}


def test_arms_must_try_each_case_the_same_attempts():
    truth = _truth(r0="REGRESSION")
    attempts = _attempts("graph", "r0", ["hit", "miss"]) + _attempts("random", "r0", ["miss"])
    with pytest.raises(ValueError, match="ATTEMPT_SET_MISMATCH"):
        aggregate(attempts, truth)


def test_more_executed_tests_per_pair_is_not_read_as_a_method_effect():
    # docs #67: an arm that runs more scenarios per pair has a larger budget, not a better method.
    truth = _truth(**{f"p{i}/{s}": "REGRESSION" for i in range(6) for s in "ab"})
    attempts = []
    for i in range(6):
        attempts += [
            _row(f"p{i}/a", "rag", 1, "COMPLETE", *HIT),
            _row(f"p{i}/b", "rag", 1, "COMPLETE", *HIT),
        ]
        attempts += [
            _row(
                f"p{i}/a", "fixed", 1, "COMPLETE", "NOT_OBSERVED_COMPLETE", "NOT_OBSERVED_COMPLETE"
            ),
            _row(f"p{i}/b", "fixed", 1, "NOT_SELECTED"),
        ]
    report = aggregate(attempts, truth)
    assert report["arms"]["rag"]["tests_per_pair_attempt"] == {"min": 2, "max": 2}
    assert report["arms"]["fixed"]["tests_per_pair_attempt"] == {"min": 1, "max": 1}
    comparison = report["comparisons"]["fixed vs rag"]
    assert comparison["budget_matched"] is False
    assert comparison["mcnemar_result"] == "예산이 달라 방법 차이로 해석하지 않음"
    assert comparison["wilcoxon_result"] == "예산이 달라 방법 차이로 해석하지 않음"


def test_scenarios_of_one_pair_are_one_unit_in_comparisons():
    # docs #67: scenarios of the same change pair are not independent samples.
    truth = _truth(**{f"p{i}/{s}": "REGRESSION" for i in range(2) for s in "abc"})
    attempts = []
    for i in range(2):
        for s in "abc":
            attempts.append(_row(f"p{i}/{s}", "graph", 1, "COMPLETE", *HIT))
            attempts.append(
                _row(
                    f"p{i}/{s}",
                    "random",
                    1,
                    "COMPLETE",
                    "NOT_OBSERVED_COMPLETE",
                    "NOT_OBSERVED_COMPLETE",
                )
            )
    report = aggregate(attempts, truth)
    assert report["arms"]["graph"]["tpr"]["detected"] == 6
    comparison = report["comparisons"]["graph vs random"]
    assert comparison["regression_cases"] == 6
    assert comparison["regression_units"] == 2
    assert (comparison["only_first_detected"], comparison["only_second_detected"]) == (2, 0)
    assert comparison["mcnemar_p"] == pytest.approx(0.5)
