"""Judge the recorded ownership runs with the team's adapter (#109) and judge (#103).

The judge and adapter are not on main yet. Run against a checkout of the adapter branch:

    git worktree add ../fd-adapter origin/feat/102-judge-adapter
    PYTHONPATH=../fd-adapter/src uv run --frozen python experiments/ownership-family/judge_family.py

Two mappings are compared, both with the unmodified judge:

- `adapter`: #109 as written. Every ORDER_READ event becomes an EFFECT.
- `returned_only`: the same input, but only events whose service outcome is RETURNED stay
  EFFECTs. A read the service refused (NOT_FOUND) disclosed nothing.

The judge supports only the plan kind `memory-delete`, so this uses it, as #109's tests do.
"""

import json
from pathlib import Path
from typing import Any

from flowdelta.ingestion import digest
from flowdelta.judge import JudgmentInput, judge
from flowdelta.judge_adapter import EffectRule, build_input

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = HERE / "evidence/runs"
INITIAL = REPO / "examples/order-lookup/state/initial.json"
RULE = EffectRule(plan_kind="memory-delete", effect_event_types=frozenset({"ORDER_READ"}))
PAIRS = [
    ("v0-service-check", "v1-internal-no-owner", "regression"),
    ("v1-internal-no-owner", "v2-internal-owner-fixed", "real fix"),
    ("v1-internal-no-owner", "v3-internal-owner-from-body", "incomplete fix"),
    ("v0-service-check", "v2-internal-owner-fixed", "refactor, no regression"),
    ("v0-service-check", "v3-internal-owner-from-body", "refactor with a hidden hole"),
]


def coarse(label: str) -> str:
    if label == "VIOLATION_OBSERVED":
        return "VIOLATION"
    if label in {"NOT_OBSERVED_COMPLETE", "BLOCKED"}:
        return "NO_VIOLATION"
    return label


def returned_only(case: JudgmentInput) -> JudgmentInput:
    kept = [
        item
        for item in case.evidence
        if item.kind != "EFFECT" or item.payload.get("outcome") == "RETURNED"
    ]
    return case.model_copy(update={"evidence": kept})


def lf_digest(path: Path) -> str:
    return digest(path.read_bytes().replace(b"\r\n", b"\n"))


def provenance_problems(run: dict[str, Any]) -> list[str]:
    """The recorded run must come from the files in this directory, byte for byte."""
    problems = []
    if run.get("runtime", {}).get("fixture_ext_sha256") != lf_digest(HERE / "fixture_ext.py"):
        problems.append("FIXTURE_SOURCE_DIFFERS")
    if run["workflow_sha256"] != lf_digest(HERE / "workflows" / f"{run['variant']}.json"):
        problems.append("WORKFLOW_DIFFERS")
    if run["initial_state_file_sha256"] != lf_digest(INITIAL):
        problems.append("INITIAL_STATE_DIFFERS")
    return problems


def main() -> None:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))["expected"]
    initial_sha = lf_digest(INITIAL)
    rows: list[dict[str, Any]] = []
    for path in sorted(RUNS.glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        arts = run["embedded_artifacts"]
        row: dict[str, Any] = {
            "variant": run["variant"],
            "input": run["input"],
            "run_id": run["run_id"],
            "execution_status": run["execution_status"],
            "http_status": arts.get("response.json", {}).get("content", {}).get("status_code"),
            "service_reads": [
                f"{event['resource_id']}:{event['outcome']}"
                for event in arts.get("events.json", {}).get("content", {}).get("events", [])
            ],
            "expected": expected[run["variant"]][run["input"]],
            "provenance_problems": provenance_problems(run),
        }
        if "events.json" not in arts:
            row.update(adapter="NO_EVIDENCE", returned_only="NO_EVIDENCE")
        else:
            case = build_input(
                run_id=run["run_id"],
                side="after",
                attempt_number=1,
                execution_status=run["execution_status"],
                events_artifact=arts["events.json"],
                final_state_artifact=arts["final-state.json"],
                request_artifact=arts["request.json"],
                rule=RULE,
                expected_initial_sha256=initial_sha,
            )
            for name, prepared in [("adapter", case), ("returned_only", returned_only(case))]:
                result = judge(prepared)
                row[name] = result.label
                row[f"{name}_reasons"] = result.reasons
        for name in ["adapter", "returned_only"]:
            row[f"{name}_matches_expected"] = coarse(row[name]) == row["expected"]
        rows.append(row)

    by_key = {(row["variant"], row["input"]): row for row in rows}
    pairs = []
    for before, after, meaning in PAIRS:
        entry: dict[str, Any] = {"before": before, "after": after, "meaning": meaning}
        for name in ["adapter", "returned_only", "expected"]:
            new = sorted(
                input_name
                for (variant, input_name), row in by_key.items()
                if variant == after
                and coarse(row[name]) == "VIOLATION"
                and coarse(by_key[(before, input_name)][name]) != "VIOLATION"
            )
            entry[f"{name}_new_violations"] = new
        pairs.append(entry)

    summary = {
        name: f"{sum(row[f'{name}_matches_expected'] for row in rows)}/{len(rows)}"
        for name in ["adapter", "returned_only"]
    }
    summary["provenance_ok"] = f"{sum(not row['provenance_problems'] for row in rows)}/{len(rows)}"
    out = {"schema_version": "0.1.0", "summary": summary, "runs": rows, "pairs": pairs}
    (HERE / "evidence/judgments.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    for row in rows:
        print(
            f"{row['variant']:30} {row['input']:14} http={row['http_status']} "
            f"reads={','.join(row['service_reads']):24} expected={row['expected']:13} "
            f"adapter={row['adapter']:22} returned_only={row['returned_only']}"
        )
    for entry in pairs:
        print(
            f"{entry['before']} -> {entry['after']} ({entry['meaning']}): "
            f"expected={entry['expected_new_violations']} "
            f"adapter={entry['adapter_new_violations']} "
            f"returned_only={entry['returned_only_new_violations']}"
        )
    print("matches expected:", summary)


if __name__ == "__main__":
    main()
