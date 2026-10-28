"""Explicitly regenerate owned metadata and reference files after a contract change."""

import argparse
import json
import shutil
import tempfile
import tomllib
from pathlib import Path

from flowdelta.additional_examples import write_support_examples
from flowdelta.contract_examples import write_contract_examples
from flowdelta.contracts import ArtifactRef, Handoff, Manifest, PolicyBindings
from flowdelta.core_adapters import ADAPTER_VERSION, ENGINE_VERSION, RUNTIME_DEFAULTS, VERSIONS
from flowdelta.fixed_profile import FixedComparisonProfile
from flowdelta.ingestion import digest, json_bytes
from flowdelta.pipeline_contracts import PipelineExamples

ROOT = Path(__file__).resolve().parents[1]


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def refresh_schemas() -> None:
    save(ROOT / "schemas/input-0.4.0.schema.json", Handoff.model_json_schema())
    save(ROOT / "schemas/pipeline-0.4.0.schema.json", PipelineExamples.model_json_schema())
    save(ROOT / "schemas/fixed-profile-v1.schema.json", FixedComparisonProfile.model_json_schema())
    save(ROOT / "schemas/policy-bindings-v1.schema.json", PolicyBindings.model_json_schema())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schemas-only", action="store_true")
    args = parser.parse_args()
    refresh_schemas()
    if args.schemas_only:
        return
    order = ROOT / "examples/order-lookup"
    case = json.loads((order / "case.json").read_bytes())
    case["dependency_manifest_ref"] = "dependencies.json"
    save(order / "case.json", case)
    lock = tomllib.loads((ROOT / "runtime/order-lookup/runtime.lock.toml").read_text())
    save(order / "dependencies.json", {
        "schema_version": "0.4.0", "before_roots": ["runtime"], "after_roots": ["runtime"],
        "dependencies": [{"dependency_id": "runtime", "kind": "RUNTIME_CONFIG", "version": "1.0.0",
                          "path": "dependencies/runtime.json", "requires": []}],
    })
    save(order / "dependencies/runtime.json", {
        "engine_version": lock["engine_version"], "image": lock["n8n_image"],
        "fixture_image": "flowdelta-order-fixture:0.2.0", "network": lock["network"],
        "fixture_source_sha256": {name: digest((ROOT / "src/flowdelta/baseline" / name).read_bytes())
                                  for name in ["fixture.py", "probe.py"]},
        "compose_sha256": digest((ROOT / "runtime/order-lookup/compose.yaml").read_bytes()),
    })
    environment = json.loads((order / "environment.json").read_bytes())
    environment["http_endpoints"] = [{"endpoint_id": "order-lookup", "method": "GET",
        "url": "http://orders-api:8080/orders/lookup", "response_fields": ["order_id", "status"],
        "response_shape": "SINGLE_JSON_OBJECT", "description_basis": "DECLARED_ONLY"}]
    save(order / "environment.json", environment)
    old_manifest = json.loads((order / "manifest.json").read_bytes())
    ids = {ref["path"]: ref["artifact_id"] for ref in old_manifest["artifacts"]}
    for path in ["dependencies.json", "dependencies/runtime.json"]:
        ids.setdefault(path, f"dependency-{len(ids)}")
    refs = [ArtifactRef(artifact_id=identity, path=path, kind="source",
                       sha256=digest((order / path).read_bytes()), size_bytes=(order / path).stat().st_size)
            for path, identity in ids.items()]
    save(order / "manifest.json", Manifest(schema_version="0.4.0", artifacts=refs).model_dump(mode="json"))
    approved = json.loads((ROOT / "runtime/order-lookup/approved-inputs.json").read_bytes())
    for path in ids:
        approved["files"]["examples/order-lookup/" + path] = ""
    approved["files"] = {path: digest((ROOT / path).read_bytes()) for path in approved["files"]}
    save(ROOT / "runtime/order-lookup/approved-inputs.json", approved)
    subsets = {
        "set": "manual assignments; flat output names; explicit constants/current or named fields; optional input preservation",
        "if": "one strict case-sensitive string/number/boolean comparison; true port 0 and false port 1",
        "merge": "append; exactly two inputs; preserves ordered items and input index",
        "splitOut": "one explicit object-array field; noOtherFields",
        "executeWorkflow": "fixed database id in manifest; once with all items; wait for child; passThrough inputs",
        "executeWorkflowTrigger": "passthrough; JSON items",
        "manualTrigger": "no input parameters other than the engine notice default",
        "webhook": "fixed GET/POST path; responseNode; none or declared header credential",
        "httpRequest": "literal declared GET endpoint; JSON response; explicit query mappings; no retry/batching",
        "respondToWebhook": "JSON response, status 200, expression evaluated against first item",
    }
    save(ROOT / "schemas/support-matrix.json", {
        "schema_version": "0.4.0", "engine_version": ENGINE_VERSION, "adapter_version": ADAPTER_VERSION,
        "profile_id": "core-local-v1", "evidence_scope": "Static subset; runtime coverage is recorded separately",
        "nodes": [{"node_type": "n8n-nodes-base." + name, "type_version": version,
                   "parameter_subset": subsets[name], "resolved_defaults": RUNTIME_DEFAULTS.get(name, {})}
                  for name, version in VERSIONS.items()],
        "unsupported": ["Code", "dynamic subworkflow references", "binary data", "AI subnodes",
                        "retryOnFail", "alwaysOutputData", "executeOnce", "non-append Merge", "legacy execution order"],
    })
    # Each producer writes a fresh directory; only the resulting owned reference files are copied.
    for name, writer in [("order-lookup", write_contract_examples), ("support-triage", write_support_examples)]:
        with tempfile.TemporaryDirectory(prefix="flowdelta-reference-") as temp:
            generated = writer(ROOT, Path(temp) / name)
            target = ROOT / "examples/contracts" / name
            for file in generated.rglob("*.json"):
                destination = target / file.relative_to(generated)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
    print("Refreshed 0.4.0 schemas, explicit runtime dependencies, and both reference bundles.")


if __name__ == "__main__":
    main()
