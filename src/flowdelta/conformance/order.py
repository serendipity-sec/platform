"""Join normal order node traces to the independent fixture's request/response records."""

import json
from pathlib import Path
from typing import Any, Literal

from flowdelta.conformance.compare import RuntimeView, evaluate
from flowdelta.conformance.observations import collect_database
from flowdelta.core_adapters import AdapterContext, describe_core, runtime_defaults
from flowdelta.ingestion import InputError, LoadedCase, digest, json_bytes, json_object
from flowdelta.normalize import normalize


def compare_order(
    loaded: LoadedCase, side: Literal["before", "after"], private: Path, artifacts: Path
) -> dict[str, Any]:
    trace = collect_database(private / "n8n/database.sqlite", artifacts / "node-observations.json")
    source = loaded.documents[loaded.case.before_ref if side == "before" else loaded.case.after_ref]
    normalized = normalize(source, "source-order-workflow")
    facts = describe_core(
        normalized, AdapterContext(loaded.environment, normalized, side, loaded.dependency_manifest)
    )
    checks: list[dict[str, Any]] = []

    def check(name: str, expected: Any, observed: Any) -> None:
        checks.append(
            {
                "check": name,
                "status": "MATCH" if expected == observed else "MISMATCH",
                "expected": expected,
                "observed": observed,
            }
        )

    executions = trace["executions"]
    check("execution-count", 1, len(executions))
    if len(executions) != 1:
        raise InputError("ORDER_EXECUTION_COUNT_MISMATCH")
    record = executions[0]
    check("execution-status", "success", record["status"])
    source_nodes = source["nodes"]
    if not isinstance(source_nodes, list):
        raise InputError("ORDER_SOURCE_NODES_INVALID")
    for key in ["nodes", "connections"]:
        expected_source = (
            [runtime_defaults(json_object(node)) for node in source_nodes]
            if key == "nodes"
            else source[key]
        )
        check(
            "source-binding:" + key,
            digest(json_bytes(expected_source)),
            digest(json_bytes(record["workflow"][key])),
        )
    view = RuntimeView(record)
    names = {node.node_id: node.name for node in normalized.nodes}
    check("all-three-nodes-observed", sorted(names.values()), sorted(view.runs))
    for fact in facts:
        check("supported:" + fact.node_id, "SUPPORTED", fact.support)
        if fact.support != "SUPPORTED":
            raise InputError("ORDER_NODE_SEMANTICS_UNSUPPORTED")
        check("node-run-count:" + fact.node_id, 1, len(view.runs.get(names[fact.node_id], [])))
    webhook = view.items(names["request"], 0, 0)
    http = view.items(names["read-order"], 0, 0)
    response = view.items(names["response"], 0, 0)
    check("webhook-item-count", 1, len(webhook))
    check("http-item-count", 1, len(http))
    check("webhook-body", loaded.task.normal_input, webhook[0]["json"].get("body"))
    by_id = {fact.node_id: fact for fact in facts}
    http_attempt = view.runs[names["read-order"]][0]
    expression = by_id["read-order"].expressions["/queryParameters/parameters/0/value"]
    query_id = evaluate(expression, webhook[0]["json"], view, http_attempt, 0)
    events = json.loads((artifacts / "events.json").read_bytes())["events"]
    check("independent-event-count", 1, len(events))
    check("http-query-binding", query_id, events[0]["resource_id"])
    check("http-method", "GET", events[0]["method"])
    check("http-response-bytes", digest(json_bytes(http[0]["json"])), events[0]["response_sha256"])
    response_attempt = view.runs[names["response"]][0]
    predicted = evaluate(
        by_id["response"].expressions["/responseBody"], http[0]["json"], view, response_attempt, 0
    )
    actual = json.loads((artifacts / "response.json").read_bytes())
    check("response-expression", predicted, actual["body"])
    check("response-code", 200, actual["status_code"])
    check(
        "response-node-passthrough",
        [item["json"] for item in http],
        [item["json"] for item in response],
    )
    report = {
        "schema_version": "0.4.0",
        "authorship": "RUNTIME_OBSERVATION",
        "side": side,
        "checks": checks,
        "all_observed_checks_match": all(check["status"] == "MATCH" for check in checks),
        "scope": "Normal request; node outputs joined to independent service events and response.",
    }
    (artifacts / "node-comparison.json").write_bytes(json_bytes(report))
    (artifacts / "extracted-semantics.json").write_bytes(
        json_bytes(
            {
                "schema_version": "0.4.0",
                "semantics": [fact.model_dump(mode="json") for fact in facts],
            }
        )
    )
    return report
