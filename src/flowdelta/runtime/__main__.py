"""`python -m flowdelta.runtime <plan-bundle> --out var/<new-dir>` from the repository root.

Also available as `flowdelta execute <plan-bundle> --out var/<new-dir>`.
"""

import argparse
import sys
from pathlib import Path

from flowdelta.ingestion import InputError, json_bytes
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.preflight import read_plan_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flowdelta.runtime")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        handoff, plan, catalog = read_plan_bundle(args.bundle)
        runs = PlanExecutor(args.bundle, Path.cwd()).execute_plan(handoff, plan, catalog, args.out)
    except InputError as exc:
        sys.stderr.write(json_bytes(exc.diagnostic()).decode("utf-8"))
        return 2
    summary = {
        "schema_version": "0.4.0",
        "authorship": "RUNTIME_OBSERVATION",
        "plan_id": plan.plan_id,
        "comparison_run_id": runs[0].comparison_run_id,
        "security_outcome": None,
        "runs": [
            {
                "file": f"runs/{run.side}-{run.attempt_number}.run.json",
                "run_id": run.run_id,
                "side": run.side,
                "execution": run.execution_status,
                "collection": run.artifacts.collection_status,
                "cleanup": run.cleanup_status,
                "error_code": run.error_code,
            }
            for run in runs
        ],
    }
    sys.stdout.write(json_bytes(summary).decode("utf-8"))
    ok = all(
        run.execution_status == "SUCCEEDED"
        and run.cleanup_status == "COMPLETE"
        and run.artifacts.collection_status == "COMPLETE"
        for run in runs
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
