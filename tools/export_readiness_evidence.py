"""Export selected normal observations. Never copy private databases or CLI stdout."""

import json
from pathlib import Path

from flowdelta.ingestion import digest, json_bytes
from flowdelta.normalize import normalize

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "runtime/readiness-evidence"


def read(path: Path):
    return json.loads(path.read_bytes())


def save(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(data))


def main() -> None:
    if TARGET.exists():
        raise SystemExit("Evidence target already exists; preserve existing runs.")
    artifacts = []
    summaries = []
    suite = ROOT / "var/conformance-001"
    for folder in sorted(path for path in suite.iterdir() if path.is_dir()):
        names = ["run.json", "runtime-executions.json", "comparison.json"]
        selected = {name: read(folder / name) for name in names}
        report = selected["comparison.json"]
        if not report["all_observed_checks_match"]:
            raise SystemExit("Conformance mismatch: " + folder.name)
        record = {"schema_version": "0.4.0", "selection": "Selected JSON observations only",
                  "source_files": {name: digest((folder / name).read_bytes()) for name in names},
                  "records": selected}
        destination = TARGET / "conformance" / (folder.name + ".json")
        save(destination, record)
        summaries.append({"case_id": folder.name, "checks": len(report["checks"]),
                          "all_match": True, "run_id": selected["run.json"]["run_id"],
                          "unobserved_nodes": len(report["not_observed_nodes"]),
                          "execution_status": selected["run.json"]["execution_status"],
                          "cleanup_status": selected["run.json"]["cleanup_status"]})
    order = ROOT / "var/baseline-readiness-002"
    if not read(order / "baseline.json")["normal_baseline_complete"]:
        raise SystemExit("Order baseline incomplete")
    for side in ["before", "after"]:
        folder = order / side
        names = ["node-observations.json", "node-comparison.json", "request.json", "response.json",
                 "events.json", "initial-state.json", "final-state.json", "runtime.json",
                 "binding.json", "cleanup.json"]
        selected = {name: read(folder / name) for name in names}
        original = read(folder / "run.json")
        run_summary = {key: value for key, value in original.items() if key != "artifacts"}
        record = {"schema_version": "0.4.0", "selection": "Selected JSON; run artifact paths omitted because logs are not exported",
                  "source_files": {name: digest((folder / name).read_bytes()) for name in [*names, "run.json"]},
                  "run_summary": run_summary, "records": selected}
        save(TARGET / "order" / (side + ".json"), record)
        report = selected["node-comparison.json"]
        summaries.append({"case_id": "order-" + side, "checks": len(report["checks"]),
                          "all_match": report["all_observed_checks_match"], "run_id": original["run_id"],
                          "unobserved_nodes": 0, "execution_status": original["execution_status"],
                          "cleanup_status": original["cleanup_status"]})
    save(TARGET / "summary.json", {"schema_version": "0.4.0", "authorship": "RUNTIME_OBSERVATION",
        "engine_version": "2.42.6", "cases": summaries, "total_checks": sum(c["checks"] for c in summaries),
        "all_observed_checks_match": all(c["all_match"] for c in summaries), "security_outcome": None,
        "business_families": 2,
        "limits": ["10 registrations/runs are not 10 independent businesses",
                   "No real A/B/C module integration", "No security detection evaluation"]})
    for path in sorted(TARGET.rglob("*.json")):
        artifacts.append({"path": path.relative_to(TARGET).as_posix(), "sha256": digest(path.read_bytes()),
                          "size_bytes": path.stat().st_size})
    save(TARGET / "manifest.json", {"schema_version": "0.4.0", "artifacts": artifacts,
         "omitted": ["private/", "SQLite databases", "credentials", "stdout.log", "stderr.log", "bootstrap.log"],
         "scope": "Evidence archive; not an A/B/C pipeline manifest"})
    public = ROOT / "examples/public-sources/customer-support"
    acquisition = read(public / "acquisition.json")
    workflows = []
    for ref in acquisition["files"]:
        path = public / ref["path"]
        assert digest(path.read_bytes()) == ref["sha256"]
        assert path.stat().st_size == ref["size_bytes"]
        if ref["path"].startswith("workflows/"):
            value = normalize(read(path), "public-source")
            workflows.append({"path": ref["path"], "nodes": len(value.nodes), "connections": len(value.connections),
                              "node_types": sorted({node.node_type for node in value.nodes}),
                              "semantic_status": "NOT_EVALUATED", "execution_status": "NOT_RUN"})
    save(public / "structure-summary.json", {"schema_version": "0.4.0", "source_commit": acquisition["commit"],
        "workflows": workflows, "total_nodes": sum(w["nodes"] for w in workflows),
        "total_connections": sum(w["connections"] for w in workflows), "families": 1,
        "production_use_evidence": "AUTHOR_DESCRIPTION_ONLY", "real_change_pair_available": False,
        "history_scope": "customer-support.json had only its initial file commit in the queried history"})
    print({"exported_artifacts": len(artifacts), "total_checks": sum(c["checks"] for c in summaries),
           "all_match": all(c["all_match"] for c in summaries)})


if __name__ == "__main__":
    main()
