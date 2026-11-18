"""Check saved evidence against integration expectations, kept outside A/B/C inputs."""

import argparse
import json
from pathlib import Path

from flowdelta.order_evaluation import comparison_outcome, judge_evaluation_bundle
from flowdelta.pipeline_io import read_manifest


def check(root: Path, repo: Path) -> dict:
    summary = json.loads((root / "summary.json").read_bytes())
    assert summary["status"] == "COMPLETE", summary["status"]
    assert len(summary["pairs"]) == 6
    run_ids, request_ids, projects = [], [], []
    counts: dict[str, int] = {}
    normal_count = 0
    trials = 0
    for pair in summary["pairs"]:
        assert {t["scenario_id"] for t in pair["trials"]} == {
            "normal",
            "other-order",
            "claimed-owner",
        }
        for trial in pair["trials"]:
            bundle = root / trial["bundle"]
            report = judge_evaluation_bundle(bundle, repo)
            assert report["records"] == trial["records"]
            outcome = comparison_outcome(report["records"])
            assert outcome == trial["comparison"]
            counts[outcome] = counts.get(outcome, 0) + 1
            trials += 1
            _, documents = read_manifest(bundle)
            for record in report["records"]:
                version = pair[record["side"]]
                scenario = trial["scenario_id"]
                forbidden = (version == "v1" and scenario != "normal") or (
                    version == "v3" and scenario == "claimed-owner"
                )
                expected = "VIOLATION_OBSERVED" if forbidden else "NOT_OBSERVED_COMPLETE"
                assert record["label"] == expected, (pair["pair_id"], scenario, record)
                assert record["execution_status"] == "SUCCEEDED"
                assert record["cleanup_status"] == "COMPLETE"
                if scenario == "normal":
                    assert record["normal_business_passed"] is True
                    normal_count += 1
                run = next(
                    d
                    for name, d in documents.items()
                    if name.endswith(".run.json") and d["run_id"] == record["run_id"]
                )
                assert run["actual_engine_version"] == "2.42.6"
                run_ids.append(record["run_id"])
                request_ids.append(record["request_id"])
                projects.append(run["extensions"]["flowdelta.compose_project"])
    assert trials == 18 and normal_count == 12
    assert len(run_ids) == len(set(run_ids)) == len(set(request_ids)) == len(set(projects)) == 36
    return {
        "status": "PASS",
        "real_runs": 36,
        "normal_business_passed": normal_count,
        "comparisons": counts,
        "scope": "SYNTHETIC_INTEGRATION_ACCEPTANCE",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            check(args.result.resolve(strict=True), Path(__file__).resolve().parents[1]), indent=2
        )
    )
