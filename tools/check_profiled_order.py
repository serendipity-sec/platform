"""Independent, read-only normal-business checks for a registered order runner output."""

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(run_root):
    bundle = run_root / "bundle"
    summary = read(run_root / "summary.json")
    expected_path = REPO / "runtime/order-lookup/expected.json"
    expected = read(expected_path)
    observations = []
    if summary["status"] != "EXECUTED":
        raise ValueError("EXECUTION_NOT_COMPLETE")
    if {run["side"] for run in summary["runs"]} != {"before", "after"}:
        raise ValueError("COMPARISON_SIDES_MISSING")
    if len(summary["runs"]) != 2 or len({run["run_id"] for run in summary["runs"]}) != 2:
        raise ValueError("COMPARISON_RUN_IDENTITIES_INVALID")
    for run in summary["runs"]:
        side = run["side"]
        run_dir = bundle / f"runs/{side}-{run['attempt_number']}"
        response = read(run_dir / "response.json")
        initial, final = read(run_dir / "initial-state.json"), read(run_dir / "final-state.json")
        events = read(run_dir / "events.json")["events"]
        checks = {
            "response_matches_preexisting_expectation": response["status_code"] == 200
                and response["body"] == expected[f"{side}_response"],
            "fresh_initial_state": initial["events"] == [] and initial["run_id"] == run["run_id"],
            "orders_unchanged": initial["orders"] == final["orders"],
            "one_owned_order_read": len(events) == expected["expected_events_per_run"]
                and all(event["event_type"] == "ORDER_READ" and event["run_id"] == run["run_id"]
                        for event in events),
            "runtime_collection_cleanup_complete": run["execution_status"] == "SUCCEEDED"
                and run["collection_status"] == run["cleanup_status"] == "COMPLETE",
        }
        if not all(checks.values()):
            raise ValueError("NORMAL_BUSINESS_CHECK_FAILED")
        observations.append({"side": side, "run_id": run["run_id"], "checks": checks})
    return {"scope": "REGISTERED_SYNTHETIC_ORDER_LOOKUP_ONLY",
            "summary_sha256": sha(run_root / "summary.json"),
            "bundle_manifest_sha256": sha(bundle / "manifest.json"),
            "preexisting_expected_sha256": sha(expected_path),
            "verification_script_sha256": sha(Path(__file__)),
            "observations": observations, "passed": True, "security_outcome": None,
            "n8n_claw_full_runtime_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = check(args.run_root)
    with args.out.open("xb") as stream:
        stream.write((json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"normal_checks": "PASS", "runs": len(report["observations"])}))
