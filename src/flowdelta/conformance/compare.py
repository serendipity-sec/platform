"""Compare extracted node facts with independent n8n observations for normal fixtures."""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from flowdelta.contracts import NodeSemantics, ValueExpression
from flowdelta.core_adapters import RUNTIME_DEFAULTS, AdapterContext, describe_core
from flowdelta.ingestion import InputError, digest, json_bytes, load_case
from flowdelta.normalize import normalize


class UnsupportedObservation(ValueError):
    pass


class ExpectedNodeError(ValueError):
    pass


class RuntimeView:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.runs = record["data"]["resultData"]["runData"]

    def items(self, name: str, run: int, port: int) -> list[dict[str, Any]]:
        return self.runs[name][run].get("data", {}).get("main", [])[port] or []

    def inputs(self, attempt: dict[str, Any]) -> list[list[dict[str, Any]]]:
        return [
            self.items(
                source["previousNode"],
                source.get("previousNodeRun", 0),
                source.get("previousNodeOutput", 0),
            )
            if source
            else []
            for source in attempt.get("source", [])
        ]

    def linked(self, target: str, attempt: dict[str, Any], item_index: int) -> dict[str, Any]:
        sources = attempt.get("source", [])
        if not sources or not sources[0]:
            raise UnsupportedObservation("NAMED_LINK_HAS_NO_SOURCE")
        source = sources[0]
        pending = [
            (
                source["previousNode"],
                source.get("previousNodeRun", 0),
                source.get("previousNodeOutput", 0),
                item_index,
            )
        ]
        found = {}
        visited: set[tuple[str, int, int, int]] = set()
        while pending:
            name, run, port, index = pending.pop()
            key = (name, run, port, index)
            if key in visited or len(visited) >= 1000:
                raise UnsupportedObservation("NAMED_LINK_CYCLE_OR_LIMIT")
            visited.add(key)
            item = self.items(name, run, port)[index]
            if name == target:
                found[key] = item["json"]
                continue
            pairs = item.get("pairedItem")
            for pair in pairs if isinstance(pairs, list) else ([] if pairs is None else [pairs]):
                pair = {"item": pair} if type(pair) is int else pair
                if not isinstance(pair, dict) or set(pair) - {"item", "input"}:
                    raise UnsupportedObservation("NAMED_LINK_UNSUPPORTED")
                previous = self.runs[name][run].get("source", [])
                if pair.get("input", 0) >= len(previous) or previous[pair.get("input", 0)] is None:
                    continue
                source = previous[pair.get("input", 0)]
                pending.append(
                    (
                        source["previousNode"],
                        source.get("previousNodeRun", 0),
                        source.get("previousNodeOutput", 0),
                        pair["item"],
                    )
                )
        if len(found) != 1:
            raise UnsupportedObservation("NAMED_LINK_MISSING_OR_AMBIGUOUS")
        return next(iter(found.values()))


def value_at(document: Any, path: list[str]) -> Any:
    result = document
    for component in path:
        if not isinstance(result, dict) or component not in result:
            raise UnsupportedObservation("MISSING_EXPRESSION_FIELD")
        result = result[component]
    return result


def evaluate(
    expression: ValueExpression,
    current: dict[str, Any],
    view: RuntimeView,
    attempt: dict[str, Any],
    item_index: int,
) -> Any:
    if expression.kind == "constant":
        return expression.value
    if expression.kind == "current_field":
        return value_at(current, expression.field_path)
    if expression.kind == "named_field":
        if expression.node_name is None:
            raise UnsupportedObservation("MISSING_NODE_NAME")
        if expression.item_selection == "FIRST":
            if len(view.runs[expression.node_name]) != 1:
                raise UnsupportedObservation("NAMED_MULTI_RUN_UNSUPPORTED")
            source = view.items(expression.node_name, 0, 0)[0]["json"]
        else:
            source = view.linked(expression.node_name, attempt, item_index)
        return value_at(source, expression.field_path)
    if expression.kind == "object":
        return {
            key: evaluate(value, current, view, attempt, item_index)
            for key, value in expression.members.items()
        }
    values = [evaluate(child, current, view, attempt, item_index) for child in expression.terms]
    if expression.kind == "concat":
        if any(not isinstance(item, str) for item in values):
            raise UnsupportedObservation("NON_STRING_CONCAT_NOT_OBSERVED")
        return "".join(values)
    if expression.kind == "compare":
        left, right = values
        if expression.comparison_type:
            types = {"number": (int, float), "string": (str,), "boolean": (bool,)}
            if any(type(item) not in types[expression.comparison_type] for item in values):
                raise ExpectedNodeError("STRICT_COMPARISON_TYPE_ERROR")
        if expression.operator in {"eq", "neq"}:
            same_type = type(left) is type(right) or (
                type(left) in (int, float) and type(right) in (int, float)
            )
            equal = same_type and left == right
            return equal if expression.operator == "eq" else not equal
        if expression.operator == "gt":
            return left > right
        if expression.operator == "gte":
            return left >= right
        if expression.operator == "lt":
            return left < right
        if expression.operator == "lte":
            return left <= right
    raise UnsupportedObservation("EXPRESSION_OBSERVATION_UNSUPPORTED")


def convert(value: Any, expected: str) -> Any:
    valid = {
        "string": (str,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
    }
    if type(value) in valid[expected]:
        return value
    if expected == "number" and isinstance(value, str):
        if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
            return float(value) if "." in value else int(value)
        raise ExpectedNodeError("INVALID_NUMBER_ASSIGNMENT")
    raise UnsupportedObservation("TYPE_CONVERSION_NOT_OBSERVED")


def _json(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item["json"] for item in items]


def expected_outputs(
    facts: NodeSemantics,
    attempt: dict[str, Any],
    view: RuntimeView,
    executions: dict[str, dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    groups = view.inputs(attempt)
    inputs = groups[0] if groups else []
    kind = facts.node_type.rsplit(".", 1)[-1]
    if kind == "manualTrigger":
        return [[{}]]
    if kind == "set":
        output = []
        for index, item in enumerate(inputs):
            fields = dict(item["json"]) if "$json.*:preserved-input" in (facts.writes or []) else {}
            for assignment in facts.assignments:
                fields[assignment.field_name] = convert(
                    evaluate(assignment.value, item["json"], view, attempt, index),
                    assignment.value_type,
                )
            output.append(fields)
        return [output]
    if kind == "if":
        predicate = facts.branches[0].predicate
        ports: list[list[dict[str, Any]]] = [[], []]
        for index, item in enumerate(inputs):
            branch = evaluate(predicate, item["json"], view, attempt, index)
            if type(branch) is not bool:
                raise UnsupportedObservation("NON_BOOLEAN_PREDICATE")
            ports[0 if branch else 1].append(item["json"])
        return ports
    if kind == "splitOut":
        expression = facts.expressions["/fieldToSplitOut"]
        flattened = []
        for index, item in enumerate(inputs):
            value = evaluate(expression, item["json"], view, attempt, index)
            if not isinstance(value, list) or any(not isinstance(child, dict) for child in value):
                raise UnsupportedObservation("SPLIT_REQUIRES_OBJECT_ARRAY_OBSERVATION")
            flattened.extend(value)
        return [flattened]
    if kind == "merge":
        return [[item["json"] for group in groups for item in group]]
    if kind == "executeWorkflow":
        metadata = attempt.get("metadata", {})
        child_id = metadata.get("subExecution", {}).get("executionId")
        if child_id not in executions or metadata.get("subExecutionsCount") != 1:
            raise UnsupportedObservation("CHILD_EXECUTION_NOT_OBSERVED")
        child = RuntimeView(executions[child_id])
        triggers = [
            n["name"]
            for n in child.record["workflow"]["nodes"]
            if n["type"] == "n8n-nodes-base.executeWorkflowTrigger"
        ]
        if len(triggers) != 1 or _json(child.items(triggers[0], 0, 0)) != _json(inputs):
            raise UnsupportedObservation("CHILD_INPUT_BINDING_MISMATCH")
        last = child.record["data"]["resultData"]["lastNodeExecuted"]
        return [_json(child.items(last, len(child.runs[last]) - 1, 0))]
    if kind == "executeWorkflowTrigger":
        parent_id = attempt.get("metadata", {}).get("parentExecution", {}).get("executionId")
        if parent_id not in executions:
            raise UnsupportedObservation("PARENT_EXECUTION_NOT_OBSERVED")
        parent = RuntimeView(executions[parent_id])
        calls = [
            call
            for runs in parent.runs.values()
            for call in runs
            if call.get("metadata", {}).get("subExecution", {}).get("executionId")
            == view.record["execution_id"]
        ]
        if len(calls) != 1:
            raise UnsupportedObservation("PARENT_CALL_NOT_UNIQUE")
        return [_json(parent.inputs(calls[0])[0])]
    raise UnsupportedObservation("NODE_OBSERVATION_NOT_IMPLEMENTED")


def compare_case(repo: Path, folder: Path) -> dict[str, Any]:
    registry = json.loads((repo / "runtime/conformance/registry.json").read_bytes())
    run = json.loads((folder / "run.json").read_bytes())
    selected = next(item for item in registry["cases"] if item["case_id"] == run["case_id"])
    trace = json.loads((folder / "runtime-executions.json").read_bytes())
    source_paths = [selected["workflow"], *registry["dependencies"]]
    sources = {
        json.loads((repo / path).read_bytes())["id"]: json.loads((repo / path).read_bytes())
        for path in source_paths
    }
    loaded = load_case(repo / "examples/support-triage")
    by_execution = {record["execution_id"]: record for record in trace["executions"]}
    checks: list[dict[str, Any]] = []
    not_observed = []
    described: list[dict[str, Any]] = []

    def check(name: str, expected: Any, observed: Any, **context: Any) -> None:
        checks.append(
            {
                "check": name,
                "status": "MATCH" if expected == observed else "MISMATCH",
                "expected": expected,
                "observed": observed,
                **context,
            }
        )

    check(
        "execution-status",
        "SUCCEEDED" if selected["expect_success"] else "FAILED",
        run["execution_status"],
    )
    check("cleanup", "COMPLETE", run["cleanup_status"])
    parent_id = json.loads((repo / selected["workflow"]).read_bytes())["id"]
    parents = [record for record in trace["executions"] if record["workflow_id"] == parent_id]
    check("parent-execution-count", 1, len(parents))
    for parent in parents:
        check(
            "parent-execution-status",
            "success" if selected["expect_success"] else "error",
            parent["status"],
        )
        runs = parent["data"]["resultData"].get("runData", {})
        check("parent-observations-present", True, bool(runs))
        check(
            "last-node-observed", True, parent["data"]["resultData"].get("lastNodeExecuted") in runs
        )
    for record in trace["executions"]:
        workflow_id = record["workflow_id"]
        if workflow_id not in sources:
            raise InputError("UNREGISTERED_OBSERVED_WORKFLOW")
        original = sources[workflow_id]
        effective_nodes = deepcopy(original["nodes"])
        for node in effective_nodes:
            for key, default in RUNTIME_DEFAULTS.get(node["type"].rsplit(".", 1)[-1], {}).items():
                node["parameters"].setdefault(key, default)
        for key in ["connections", "nodes"]:
            check(
                "runtime-source-binding:" + key,
                effective_nodes if key == "nodes" else original[key],
                record["workflow"][key],
                execution_id=record["execution_id"],
            )
        normalized = normalize(original, "conformance-source")
        facts_list = describe_core(
            normalized,
            AdapterContext(loaded.environment, normalized, "before", loaded.dependency_manifest),
        )
        described.extend(item.model_dump(mode="json") for item in facts_list)
        view = RuntimeView(record)
        names = {node.node_id: node.name for node in normalized.nodes}
        for facts in facts_list:
            name = names[facts.node_id]
            identity = {
                "execution_id": record["execution_id"],
                "workflow_id": workflow_id,
                "node_id": facts.node_id,
            }
            if name not in view.runs:
                not_observed.append(identity)
                continue
            if facts.support != "SUPPORTED":
                checks.append(
                    {
                        "check": "node-semantics",
                        "status": "UNSUPPORTED",
                        "reason": facts.reason,
                        **identity,
                    }
                )
                continue
            for index, attempt in enumerate(view.runs[name]):
                identity["node_run_index"] = index
                actual = [_json(items or []) for items in attempt.get("data", {}).get("main", [])]
                try:
                    expected = expected_outputs(facts, attempt, view, by_execution)
                    check("node-output-values-and-ports", expected, actual, **identity)
                    groups = view.inputs(attempt)
                    for output_index, items in enumerate(attempt.get("data", {}).get("main", [])):
                        for item in items or []:
                            pairs = item.get("pairedItem")
                            for pair in (
                                pairs
                                if isinstance(pairs, list)
                                else ([] if pairs is None else [pairs])
                            ):
                                pair = {"item": pair} if type(pair) is int else pair
                                if groups and isinstance(pair, dict):
                                    input_index = pair.get("input", 0)
                                    valid = input_index < len(groups) and 0 <= pair.get(
                                        "item", -1
                                    ) < len(groups[input_index])
                                    check(
                                        "item-link-in-input-range",
                                        True,
                                        valid,
                                        output_index=output_index,
                                        **identity,
                                    )
                except ExpectedNodeError:
                    mode = facts.error_behavior.on_error if facts.error_behavior else "stopWorkflow"
                    if mode == "stopWorkflow":
                        check(
                            "expected-node-error",
                            "error",
                            attempt.get("executionStatus"),
                            **identity,
                        )
                    else:
                        port = 1 if mode == "continueErrorOutput" else 0
                        error_items = actual[port] if len(actual) > port else []
                        check(
                            "expected-error-output-port",
                            True,
                            len(error_items) == 1 and "error" in error_items[0],
                            **identity,
                        )
                except (UnsupportedObservation, KeyError, IndexError, TypeError) as exc:
                    checks.append(
                        {
                            "check": "node-output-values-and-ports",
                            "status": "UNSUPPORTED",
                            "reason": str(exc),
                            **identity,
                        }
                    )
    # Source binding values are large. Retain their hashes after comparing actual contents.
    for item in checks:
        if item["check"].startswith("runtime-source-binding:"):
            item["expected_sha256"] = digest(json_bytes(item.pop("expected")))
            item["observed_sha256"] = digest(json_bytes(item.pop("observed")))
    report = {
        "schema_version": "0.4.0",
        "case_id": run["case_id"],
        "run_id": run["run_id"],
        "authorship": "RUNTIME_OBSERVATION",
        "checks": checks,
        "not_observed_nodes": not_observed,
        "all_observed_checks_match": bool(checks)
        and all(item["status"] == "MATCH" for item in checks),
        "scope": "Normal node outputs, ports, source binding and item links; no security verdict.",
    }
    (folder / "extracted-semantics.json").write_bytes(json_bytes({"semantics": described}))
    (folder / "comparison.json").write_bytes(json_bytes(report))
    return report
