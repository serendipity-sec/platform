import json
from pathlib import Path

from flowdelta.conformance.compare import compare_case
from flowdelta.ingestion import digest, json_bytes

ROOT = Path(__file__).resolve().parents[1]


def test_selected_evidence_and_public_sources_retain_recorded_bytes() -> None:
    for folder, manifest_name, field in [
        (ROOT / "runtime/readiness-evidence", "manifest.json", "artifacts"),
        (ROOT / "examples/public-sources/customer-support", "acquisition.json", "files"),
    ]:
        manifest = json.loads((folder / manifest_name).read_bytes())
        for ref in manifest[field]:
            path = folder / ref["path"]
            assert "private" not in path.parts
            assert digest(path.read_bytes()) == ref["sha256"]
            assert path.stat().st_size == ref["size_bytes"]


def test_runtime_comparison_detects_changed_recorded_output(tmp_path: Path) -> None:
    saved = json.loads(
        (ROOT / "runtime/readiness-evidence/conformance/support-before.json").read_bytes()
    )
    for name in ["run.json", "runtime-executions.json"]:
        (tmp_path / name).write_bytes(json_bytes(saved["records"][name]))
    assert compare_case(ROOT, tmp_path)["all_observed_checks_match"]
    trace = saved["records"]["runtime-executions.json"]
    parent = next(
        record for record in trace["executions"] if record["workflow_id"] == "fdSupportTriage"
    )
    runs = parent["data"]["resultData"]["runData"]
    last = parent["data"]["resultData"]["lastNodeExecuted"]
    runs[last][0]["data"]["main"][0][0]["json"]["ticket_id"] = "unexpected-normal-output"
    (tmp_path / "runtime-executions.json").write_bytes(json_bytes(trace))
    changed = compare_case(ROOT, tmp_path)
    assert not changed["all_observed_checks_match"]
    assert any(
        check["check"] == "node-output-values-and-ports" and check["status"] == "MISMATCH"
        for check in changed["checks"]
    )
