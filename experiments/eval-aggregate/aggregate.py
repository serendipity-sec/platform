"""Aggregate FlowDelta evaluation attempts into a reproducible table (docs #56 3-3, #70 6).

Input
- `--attempts`: JSON list of attempt records, one per (case, arm, attempt):
  {"case_id": "R1/foreign", "arm": "graph+retrieval", "attempt": 1,
   "before": <judge label>, "after": <judge label>}
  `case_id` is `<change pair>/<scenario>`. Judge labels are the #103 values: VIOLATION_OBSERVED,
  BLOCKED, NOT_OBSERVED_COMPLETE, INCONCLUSIVE, UNSUPPORTED. A missing side is written as null.
  Records written by `flowdelta run-evaluation-attempt` (#125) also carry `status` and `runs`
  ([{"side", "run_id", ...}]); both are optional for hand-built records.
- `--truth`: JSON {"cases": [{"case_id": ..., "truth": "REGRESSION" | "NO_REGRESSION" |
  "INCONCLUSIVE"}]}. Build it from sealed label files with `truth_from_labels()` only after
  every labeler has submitted (docs #70).

Rules fixed before results are seen (docs research 56 pre-registration draft, platform #128)
- An attempt CONFIRMS a regression when after = VIOLATION_OBSERVED and before is a complete
  no-violation result (NOT_OBSERVED_COMPLETE or BLOCKED). A violation on both sides is an
  existing risk, not a new regression.
- INCONCLUSIVE, UNSUPPORTED or a missing side is never counted as a detection or as safe. It stays
  in every denominator and is reported separately. Record status keeps "not selected by the arm",
  "selected but not executed" and "arm unsupported" apart from "judged inconclusive".
- Ground truth needs both sides labeled. A violation after a before side labeled "근거 부족" is
  INCONCLUSIVE, not a confirmed regression.
- A case is detected by an arm when at least one of its attempts confirms (budget = attempts).
  Every compared arm must have the same attempt numbers for every case, one record each, and one
  raw run may back only one record.
- Arms are compared with exact paired tests on change pairs, because scenarios of one pair share
  the same workflows and are not independent: McNemar on per-pair detection and Wilcoxon
  signed-rank on per-pair confirmation rates, then Holm correction across comparisons.
- Arms that ran more scenarios per pair and attempt than the other arm are reported, but the
  difference is not read as a method effect.

Standard library only. The output is deterministic for the same input bytes.
"""

import argparse
import hashlib
import itertools
import json
import math
from collections import defaultdict
from pathlib import Path

NO_VIOLATION = {"NOT_OBSERVED_COMPLETE", "BLOCKED"}
UNKNOWN = {"INCONCLUSIVE", "UNSUPPORTED", None}
LABELS = {"위반 확정", "위반 없음", "근거 부족"}
# Record statuses whose before/after labels come from a judged trial. None = hand-built record.
JUDGED = {None, "COMPLETE", "INCOMPLETE"}
# Records that did not spend any of the arm's test budget.
NO_BUDGET = {"NOT_SELECTED", "UNSUPPORTED"}
OUTCOMES = (
    "CONFIRMED",
    "NOT_CONFIRMED",
    "INCONCLUSIVE",
    "NOT_SELECTED",
    "NOT_EXECUTED",
    "UNSUPPORTED",
)
ALPHA = 0.05
BUDGET_MISMATCH = "예산이 달라 방법 차이로 해석하지 않음"


def attempt_outcome(before: str | None, after: str | None) -> str:
    if before in UNKNOWN or after in UNKNOWN:
        return "INCONCLUSIVE"
    if after == "VIOLATION_OBSERVED" and before in NO_VIOLATION:
        return "CONFIRMED"
    return "NOT_CONFIRMED"


def record_outcome(record: dict) -> str:
    """Outcome of one record; only a judged trial can confirm or rule out a regression."""
    status = record.get("status")
    if status in JUDGED:
        return attempt_outcome(record.get("before"), record.get("after"))
    if status in NO_BUDGET:
        return status
    return "NOT_EXECUTED"  # FAILED, INTERRUPTED, NOT_RUN, PREPARED, ...


def change_pair(case_id: str) -> str:
    return case_id.rsplit("/", 1)[0]


def truth_from_labels(labels: dict) -> dict:
    """docs #70 labels (위반 확정 / 위반 없음 / 근거 부족) per side → per-case truth."""
    sides: dict[str, dict[str, str]] = defaultdict(dict)
    for case in labels["cases"]:
        case_id, side = f"{case['pair_id']}/{case['scenario']}", case["side"]
        if case["label"] not in LABELS:
            raise ValueError(f"UNKNOWN_LABEL: {case_id} {side} {case['label']!r}")
        if side in sides[case_id]:
            raise ValueError(f"DUPLICATE_LABEL: {case_id} {side}")
        sides[case_id][side] = case["label"]
    cases = []
    for case_id, label in sorted(sides.items()):
        missing = [side for side in ("before", "after") if side not in label]
        if missing:
            raise ValueError(f"LABEL_MISSING: {case_id} {','.join(missing)}")
        before, after = label["before"], label["after"]
        if after == "근거 부족" or (after == "위반 확정" and before == "근거 부족"):
            truth = "INCONCLUSIVE"
        elif after == "위반 확정" and before == "위반 없음":
            truth = "REGRESSION"
        else:
            truth = "NO_REGRESSION"
        cases.append({"case_id": case_id, "truth": truth})
    return {"cases": cases}


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total == 0:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def wilcoxon_exact(differences: list[float]) -> float:
    """Two-sided exact Wilcoxon signed-rank p-value; zero differences are dropped."""
    nonzero = [d for d in differences if d != 0]
    n = len(nonzero)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(nonzero[i]))
    ranks = [0.0] * n
    position = 0
    while position < n:
        end = position
        while end + 1 < n and abs(nonzero[order[end + 1]]) == abs(nonzero[order[position]]):
            end += 1
        for index in order[position : end + 1]:
            ranks[index] = (position + end + 2) / 2
        position = end + 1
    observed = sum(r for r, d in zip(ranks, nonzero, strict=True) if d > 0)
    centre = sum(ranks) / 2
    extreme = 0
    for signs in itertools.product((0, 1), repeat=n):
        statistic = sum(r for r, s in zip(ranks, signs, strict=True) if s)
        if abs(statistic - centre) >= abs(observed - centre) - 1e-12:
            extreme += 1
    return extreme / 2**n


def holm(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted, running = {}, 0.0
    for index, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - index) * p))
        adjusted[name] = running
    return adjusted


def _check_records(attempts: list[dict], truth_by_case: dict[str, str]) -> None:
    seen: set[tuple] = set()
    run_owner: dict[str, tuple] = {}
    for record in attempts:
        key = (record["case_id"], record["arm"], record["attempt"])
        if record["case_id"] not in truth_by_case:
            raise ValueError(f"CASE_WITHOUT_TRUTH: {record['case_id']}")
        if key in seen:
            raise ValueError(f"DUPLICATE_ATTEMPT: {key}")
        seen.add(key)
        for run in record.get("runs") or []:
            run_id = run.get("run_id")
            if run_id is None:
                continue
            if run_id in run_owner:
                raise ValueError(f"SHARED_OBSERVATION: {run_id} in {run_owner[run_id]} and {key}")
            run_owner[run_id] = key


def aggregate(attempts: list[dict], truth: dict) -> dict:
    truth_by_case = {case["case_id"]: case["truth"] for case in truth["cases"]}
    _check_records(attempts, truth_by_case)
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for record in attempts:
        by_arm[record["arm"]].append(record)
    unsupported_arms = sorted(
        arm
        for arm, records in by_arm.items()
        if all(r.get("status") == "UNSUPPORTED" for r in records)
    )
    arms = sorted(arm for arm in by_arm if arm not in unsupported_arms)
    cases = sorted(truth_by_case)
    for case in cases:
        attempt_sets = {
            arm: sorted(r["attempt"] for r in by_arm[arm] if r["case_id"] == case) for arm in arms
        }
        if len({tuple(numbers) for numbers in attempt_sets.values()}) > 1:
            raise ValueError(f"ATTEMPT_SET_MISMATCH: {case} {attempt_sets}")
    positives = [c for c in cases if truth_by_case[c] == "REGRESSION"]
    negatives = [c for c in cases if truth_by_case[c] == "NO_REGRESSION"]
    pairs: dict[str, list[str]] = defaultdict(list)
    for case in positives:
        pairs[change_pair(case)].append(case)
    report_arms = {}
    detected: dict[str, dict[str, bool]] = {}
    rates: dict[str, dict[str, float]] = {}
    for arm in arms:
        per_case: dict[str, list[str]] = defaultdict(list)
        tests: dict[tuple, int] = defaultdict(int)
        for record in by_arm[arm]:
            per_case[record["case_id"]].append(record_outcome(record))
            tests[(change_pair(record["case_id"]), record["attempt"])] += (
                record.get("status") not in NO_BUDGET
            )
        flat = [o for case in cases for o in per_case.get(case, [])]
        statuses = [r.get("status") or "UNSPECIFIED" for r in by_arm[arm]]
        detected[arm] = {c: "CONFIRMED" in per_case.get(c, []) for c in cases}
        rates[arm] = {
            c: (per_case[c].count("CONFIRMED") / len(per_case[c])) if per_case.get(c) else 0.0
            for c in cases
        }
        tp = sum(detected[arm][c] for c in positives)
        fp = sum(detected[arm][c] for c in negatives)
        report_arms[arm] = {
            "cases": len(cases),
            "cases_without_attempts": [case for case in cases if case not in per_case],
            "attempts": len(flat),
            "attempt_outcomes": {k: flat.count(k) for k in OUTCOMES},
            "record_statuses": {s: statuses.count(s) for s in sorted(set(statuses))},
            "tests_per_pair_attempt": {"min": min(tests.values()), "max": max(tests.values())},
            "tpr": {"detected": tp, "of": len(positives), "wilson_95": wilson(tp, len(positives))},
            "fpr": {"alarms": fp, "of": len(negatives), "wilson_95": wilson(fp, len(negatives))},
            "inconclusive_truth_cases": sum(truth_by_case[c] == "INCONCLUSIVE" for c in cases),
        }
    raw = {}
    comparisons = {}
    for a, b in itertools.combinations(arms, 2):
        hit = {
            arm: {p: any(detected[arm][c] for c in members) for p, members in pairs.items()}
            for arm in (a, b)
        }
        rate = {
            arm: {
                p: sum(rates[arm][c] for c in members) / len(members)
                for p, members in pairs.items()
            }
            for arm in (a, b)
        }
        only_a = sum(hit[a][p] and not hit[b][p] for p in pairs)
        only_b = sum(hit[b][p] and not hit[a][p] for p in pairs)
        differences = [rate[a][p] - rate[b][p] for p in sorted(pairs)]
        key = f"{a} vs {b}"
        comparisons[key] = {
            "regression_cases": len(positives),
            "regression_units": len(pairs),
            "budget_matched": report_arms[a]["tests_per_pair_attempt"]["max"]
            == report_arms[b]["tests_per_pair_attempt"]["max"],
            "only_first_detected": only_a,
            "only_second_detected": only_b,
            "mcnemar_p": mcnemar_exact(only_a, only_b),
            "wilcoxon_p": wilcoxon_exact(differences),
            "nonzero_rate_differences": sum(d != 0 for d in differences),
        }
        raw[f"{key} / mcnemar"] = comparisons[key]["mcnemar_p"]
        raw[f"{key} / wilcoxon"] = comparisons[key]["wilcoxon_p"]
    adjusted = holm(raw)
    for key, values in comparisons.items():
        for test in ("mcnemar", "wilcoxon"):
            p = adjusted[f"{key} / {test}"]
            values[f"{test}_p_holm"] = p
            if not values["budget_matched"]:
                values[f"{test}_result"] = BUDGET_MISMATCH
            else:
                values[f"{test}_result"] = (
                    "차이 확인" if p < ALPHA else "이 규모에서 차이를 확인하지 못함"
                )
    return {
        "schema": "flowdelta-eval-aggregate-v1",
        "alpha": ALPHA,
        "arms": report_arms,
        "unsupported_arms": unsupported_arms,
        "comparisons": comparisons,
        "note": "Comparisons use change pairs as units. Exact two-sided tests need at least 6 "
        "discordant pairs (McNemar) or 6 nonzero differences (Wilcoxon) to reach p < 0.05; "
        "smaller sets are descriptive only.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    attempts_bytes, truth_bytes = args.attempts.read_bytes(), args.truth.read_bytes()
    report = aggregate(json.loads(attempts_bytes), json.loads(truth_bytes))
    report["inputs"] = {
        "attempts_sha256": hashlib.sha256(attempts_bytes).hexdigest(),
        "truth_sha256": hashlib.sha256(truth_bytes).hexdigest(),
        # LF-normalized so a Windows CRLF checkout records the same script identity.
        "script_sha256": hashlib.sha256(
            Path(__file__).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest(),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
