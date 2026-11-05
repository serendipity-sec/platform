"""Independent normal-business checks over manifest-verified runtime observations."""

from pathlib import Path
from typing import Any

from flowdelta.ingestion import InputError, digest, parse_file_model, parse_json
from flowdelta.module_outputs import read_module_outputs
from flowdelta.pipeline_contracts import ExecutionRun
from flowdelta.pipeline_io import read_manifest


def normal_checks(
    run: ExecutionRun,
    expected: dict[str, Any],
    normal_input: dict[str, Any],
    response: dict[str, Any],
    initial: dict[str, Any],
    final: dict[str, Any],
    event_document: dict[str, Any],
) -> dict[str, bool]:
    events = event_document.get("events", [])
    return {
        "execution_succeeded": run.execution_status == "SUCCEEDED",
        "collection_complete": run.artifacts.collection_status == "COMPLETE",
        "cleanup_complete": run.cleanup_status == "COMPLETE",
        "expected_response": response.get("status_code") == 200
        and response.get("body") == expected[f"{run.side}_response"],
        "expected_input": normal_input == expected["normal_input"],
        "fresh_state": initial.get("events") == [] and initial.get("run_id") == run.run_id,
        "final_state_owned": final.get("run_id") == run.run_id,
        "orders_unchanged": "orders" in initial and initial["orders"] == final.get("orders"),
        "one_owned_read": len(events) == expected["expected_events_per_run"]
        and all(
            event.get("event_type") == "ORDER_READ"
            and event.get("run_id") == run.run_id
            and event.get("resource_id") == expected["normal_input"]["order_id"]
            and event.get("outcome") == "RETURNED"
            for event in events
        ),
    }


def verify_profiled_order(run_root: Path, repo: Path) -> dict[str, Any]:
    bundle = run_root / "bundle"
    summary: dict[str, Any] = parse_json((run_root / "summary.json").read_bytes())
    read_module_outputs(bundle)
    _, docs = read_manifest(bundle)
    runs = [
        parse_file_model(ExecutionRun, value)
        for path, value in docs.items()
        if path.startswith("runs/") and path.endswith(".run.json")
    ]
    actual = sorted((run.run_id, run.side, run.attempt_number) for run in runs)
    reported = summary.get("runs")
    if not isinstance(reported, list) or not all(isinstance(run, dict) for run in reported):
        raise InputError("COMPARISON_RUN_IDENTITIES_INVALID")
    reported_ids = [
        (run.get("run_id"), run.get("side"), run.get("attempt_number")) for run in reported
    ]
    if (
        len(actual) != 2
        or {(run.side, run.attempt_number) for run in runs} != {("before", 1), ("after", 1)}
        or any(identity not in actual for identity in reported_ids)
        or len(reported_ids) != len({str(identity) for identity in reported_ids})
        or len(reported_ids) != len(actual)
    ):
        raise InputError("COMPARISON_RUN_IDENTITIES_INVALID")
    manifest_hash = digest((bundle / "manifest.json").read_bytes())
    if summary.get("bundle_manifest_sha256") != manifest_hash:
        raise InputError("SUMMARY_BUNDLE_MISMATCH")
    if summary.get("status") != "EXECUTED":
        raise InputError("EXECUTION_NOT_COMPLETE")
    expected_path = repo / "runtime/order-lookup/expected.json"
    expected: dict[str, Any] = parse_json(expected_path.read_bytes())
    plan: dict[str, Any] = docs["plan.json"]
    observations: list[dict[str, Any]] = []
    for run in runs:
        by_id = {ref.artifact_id: ref for ref in run.artifacts.artifacts}

        documents: dict[str, Any] = {
            kind: docs.get(by_id[identity].path, {})
            for kind, identity in run.artifacts.artifact_kinds.items()
            if identity in by_id
        }

        checks = normal_checks(
            run,
            expected,
            plan["normal_input"],
            documents.get("response", {}),
            documents.get("initial-state", {}),
            documents.get("final-state", {}),
            documents.get("events", {}),
        )
        observations.append({"side": run.side, "run_id": run.run_id, "checks": checks})
    if not all(all(observation["checks"].values()) for observation in observations):
        raise InputError("NORMAL_BUSINESS_CHECK_FAILED")
    return {
        "scope": "REGISTERED_SYNTHETIC_ORDER_LOOKUP_ONLY",
        "summary_sha256": digest((run_root / "summary.json").read_bytes()),
        "bundle_manifest_sha256": manifest_hash,
        "preexisting_expected_sha256": digest(expected_path.read_bytes()),
        "verification_source_sha256": digest(Path(__file__).read_bytes()),
        "observations": observations,
        "passed": True,
        "security_outcome": None,
        "n8n_claw_full_runtime_verified": False,
    }
