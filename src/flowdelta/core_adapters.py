"""Versioned, per-node facts for a bounded n8n subset. Never runs source expressions."""

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue, ValidationError

from flowdelta.contracts import (
    BranchCondition,
    DependencyManifest,
    Environment,
    ErrorBehavior,
    FieldAssignment,
    ItemBehavior,
    NodeSemantics,
    NormalizedNode,
    NormalizedWorkflow,
    SourceLocation,
    ValueExpression,
)
from flowdelta.dependencies import dependency_closure, fixed_workflow_id
from flowdelta.expressions import UnsupportedExpression, parse_expression
from flowdelta.ingestion import InputError, json_object, parse_model

CORE_PROFILE = "core-local-v1"
ENGINE_VERSION = "2.42.6"
ADAPTER_VERSION = "core-adapters-v1"
VERSIONS = {
    "manualTrigger": 1,
    "webhook": 2.2,
    "respondToWebhook": 1.5,
    "httpRequest": 4.5,
    "set": 3.5,
    "if": 2.3,
    "merge": 3.2,
    "splitOut": 1,
    "executeWorkflow": 1.4,
    "executeWorkflowTrigger": 1.2,
}

# Defaults observed in the pinned engine's node metadata and execution workflow export.
RUNTIME_DEFAULTS: dict[str, dict[str, JsonValue]] = {
    "manualTrigger": {"notice": ""},
    "set": {"duplicateItem": False},
    "executeWorkflow": {"operation": "call_workflow"},
    "executeWorkflowTrigger": {"events": "worklfow_call"},
    "webhook": {"multipleMethods": False, "webhookNotice": ""},
    "httpRequest": {
        "curlImport": "",
        "infoMessage": "",
        "provideSslCertificates": False,
        "sendBody": False,
        "sendHeaders": False,
        "specifyQuery": "keypair",
    },
    "respondToWebhook": {"enableResponseOutput": False, "generalNotice": "", "webhookNotice": ""},
}
RUNTIME_NESTED_DEFAULTS: dict[str, dict[str, JsonValue]] = {
    "httpRequest": {
        "/options/response/response/fullResponse": False,
        "/options/response/response/neverError": False,
    },
}


def runtime_defaults(node: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Materialize only observed engine defaults, preserving every explicit source value."""
    result = deepcopy(node)
    kind = str(node["type"]).rsplit(".", 1)[-1]
    parameters = json_object(result["parameters"])
    for key, value in RUNTIME_DEFAULTS.get(kind, {}).items():
        parameters.setdefault(key, value)
    for pointer, value in RUNTIME_NESTED_DEFAULTS.get(kind, {}).items():
        parts = pointer.split("/")[1:]
        parent: JsonValue = parameters
        for part in parts[:-1]:
            parent = parent.get(part) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.setdefault(parts[-1], value)
    return result


class UnsupportedNode(ValueError):
    def __init__(self, reason: str, path: str = "/parameters") -> None:
        self.reason, self.path = reason, path


@dataclass(frozen=True)
class AdapterContext:
    environment: Environment
    workflow: NormalizedWorkflow
    side: Literal["before", "after"]
    dependencies: DependencyManifest | None


def _only(values: dict[str, JsonValue], allowed: set[str]) -> None:
    if set(values) - allowed:
        raise UnsupportedNode("PARAMETER_SUBSET_NOT_SUPPORTED")


def _value(value: JsonValue, context: AdapterContext) -> ValueExpression:
    if isinstance(value, str) and value.startswith("="):
        expression = parse_expression(value).to_contract()
        pending = [expression]
        names = {node.name for node in context.workflow.nodes}
        while pending:
            current = pending.pop()
            if current.kind == "named_field" and current.node_name not in names:
                raise UnsupportedNode("NAMED_NODE_NOT_FOUND")
            pending.extend([*current.terms, *current.members.values()])
        return expression
    return ValueExpression(kind="constant", value=value)


def _reads(value: ValueExpression) -> list[str]:
    if value.kind == "current_field":
        return ["$json" + "".join(f".{name}" for name in value.field_path)]
    if value.kind == "named_field":
        mode = "item" if value.item_selection == "LINKED" else "first()"
        return [f"$({value.node_name!r}).{mode}.json" + "".join(f".{p}" for p in value.field_path)]
    return sorted(
        {read for child in [*value.terms, *value.members.values()] for read in _reads(child)}
    )


def _behavior(
    processing: str, cardinality: str, lineage: str, inputs: int, outputs: int
) -> ItemBehavior:
    return parse_model(
        ItemBehavior,
        {
            "processing": processing,
            "cardinality": cardinality,
            "lineage": lineage,
            "input_ports": inputs,
            "output_ports": outputs,
            "note": "JSON items only; field existence and runtime item links require observations.",
        },
    )


def _settings(node: NormalizedNode, output_ports: int) -> ErrorBehavior:
    values = node.opaque_fields
    allowed = {
        "position",
        "credentials",
        "webhookId",
        "notes",
        "notesInFlow",
        "disabled",
        "onError",
        "retryOnFail",
        "maxTries",
        "waitBetweenTries",
        "alwaysOutputData",
        "executeOnce",
    }
    if set(values) - allowed or values.get("disabled", False) is not False:
        raise UnsupportedNode("NODE_SETTINGS_NOT_SUPPORTED", "")
    if (
        values.get("retryOnFail", False)
        or values.get("alwaysOutputData", False)
        or values.get("executeOnce", False)
    ):
        raise UnsupportedNode("RETRY_OR_BATCH_OVERRIDE_NOT_SUPPORTED", "")
    if "maxTries" in values or "waitBetweenTries" in values:
        raise UnsupportedNode("RETRY_SETTINGS_NOT_SUPPORTED", "")
    mode = values.get("onError", "stopWorkflow")
    return parse_model(
        ErrorBehavior,
        {
            "on_error": mode,
            "error_output_index": output_ports if mode == "continueErrorOutput" else None,
        },
    )


def describe_core_node(node: NormalizedNode, context: AdapterContext) -> NodeSemantics:
    result = NodeSemantics(
        node_id=node.node_id,
        side=context.side,
        node_type=node.node_type,
        type_version=node.type_version,
        operation=node.parameters.get("operation"),
        source=node.source,
        engine_version=context.environment.target_version,
        adapter_version=ADAPTER_VERSION,
        support="UNSUPPORTED",
        reason="NODE_TYPE_VERSION_NOT_SUPPORTED",
    )
    try:
        if context.environment.target_version != ENGINE_VERSION:
            raise UnsupportedNode("ENGINE_VERSION_NOT_SUPPORTED", "")
        kind = node.node_type.removeprefix("n8n-nodes-base.")
        if (
            not node.node_type.startswith("n8n-nodes-base.")
            or VERSIONS.get(kind) != node.type_version
        ):
            raise UnsupportedNode("NODE_TYPE_VERSION_NOT_SUPPORTED", "/typeVersion")
        p = deepcopy(node.parameters)
        resolved_defaults = {}
        for key, default in RUNTIME_DEFAULTS.get(kind, {}).items():
            if key in p and (p[key] != default or type(p[key]) is not type(default)):
                raise UnsupportedNode("NODE_DEFAULT_OVERRIDE_NOT_SUPPORTED")
            if key not in p:
                resolved_defaults[key] = default
            p.pop(key, None)
        for pointer, default in RUNTIME_NESTED_DEFAULTS.get(kind, {}).items():
            parts = pointer.split("/")[1:]
            parent: JsonValue = p
            for part in parts[:-1]:
                parent = parent.get(part) if isinstance(parent, dict) else None
            if isinstance(parent, dict):
                key = parts[-1]
                if key in parent and (
                    parent[key] != default or type(parent[key]) is not type(default)
                ):
                    raise UnsupportedNode(
                        "NODE_DEFAULT_OVERRIDE_NOT_SUPPORTED", "/parameters" + pointer
                    )
                if key not in parent:
                    resolved_defaults[pointer] = default
                parent.pop(key, None)
        expressions: dict[str, ValueExpression] = {}
        writes: list[str] = []
        effects: list[str] = []
        capabilities: list[str] = []
        branches: list[BranchCondition] = []
        assignments_result: list[FieldAssignment] = []
        dependencies: list[str] = []
        behavior = _behavior("PER_ITEM", "ONE_PER_INPUT", "PRESERVE", 1, 1)
        if kind == "manualTrigger":
            _only(p, set())
            behavior = _behavior("SINGLE_REQUEST", "ONE", "SOURCE", 0, 1)
        elif kind == "set":
            _only(p, {"mode", "assignments", "includeOtherFields", "options"})
            if p.get("mode", "manual") != "manual" or p.get("options", {}) != {}:
                raise UnsupportedNode("SET_MODE_NOT_SUPPORTED")
            if type(p.get("includeOtherFields", False)) is not bool:
                raise UnsupportedNode("SET_MODE_NOT_SUPPORTED")
            assignments = json_object(p.get("assignments", {})).get("assignments")
            if not isinstance(assignments, list) or not assignments:
                raise UnsupportedNode("SET_ASSIGNMENTS_REQUIRED")
            names: set[str] = set()
            for index, raw in enumerate(assignments):
                assignment = json_object(raw)
                _only(assignment, {"id", "name", "value", "type"})
                name = assignment.get("name")
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    or name in names
                ):
                    raise UnsupportedNode("SET_FIELD_NAME_NOT_SUPPORTED")
                if assignment.get("type") not in {"string", "number", "boolean", "array", "object"}:
                    raise UnsupportedNode("SET_TYPE_NOT_SUPPORTED")
                names.add(name)
                expressions[f"/assignments/assignments/{index}/value"] = _value(
                    assignment.get("value"), context
                )
                writes.append(f"$json.{name}")
                assignments_result.append(
                    parse_model(
                        FieldAssignment,
                        {
                            "field_name": name,
                            "value_type": assignment["type"],
                            "value": expressions[
                                f"/assignments/assignments/{index}/value"
                            ].model_dump(mode="json"),
                            "source": SourceLocation(
                                artifact_id=node.source.artifact_id,
                                json_pointer=f"{node.source.json_pointer}/parameters/assignments/assignments/{index}",
                            ).model_dump(mode="json"),
                        },
                    )
                )
            if p.get("includeOtherFields", False):
                writes.append("$json.*:preserved-input")
        elif kind == "if":
            _only(p, {"conditions", "looseTypeValidation", "options"})
            conditions = json_object(p.get("conditions", {}))
            _only(conditions, {"options", "conditions", "combinator"})
            options = json_object(conditions.get("options", {}))
            _only(options, {"caseSensitive", "leftValue", "typeValidation", "version"})
            if (
                p.get("options", {}) != {}
                or p.get("looseTypeValidation", False) is not False
                or options.get("caseSensitive") is not True
                or options.get("typeValidation") != "strict"
            ):
                raise UnsupportedNode("IF_COMPARISON_OPTIONS_NOT_SUPPORTED")
            comparisons = conditions.get("conditions")
            if not isinstance(comparisons, list) or len(comparisons) != 1:
                raise UnsupportedNode("IF_SINGLE_CONDITION_REQUIRED")
            condition = json_object(comparisons[0])
            _only(condition, {"id", "leftValue", "rightValue", "operator"})
            operator = json_object(condition.get("operator"))
            _only(operator, {"type", "operation", "singleValue"})
            if operator.get("type") not in {"string", "number", "boolean"} or operator.get(
                "operation"
            ) not in {"equals", "notEquals", "gt", "gte", "lt", "lte"}:
                raise UnsupportedNode("IF_OPERATOR_NOT_SUPPORTED")
            mapping = {
                "equals": "eq",
                "notEquals": "neq",
                "gt": "gt",
                "gte": "gte",
                "lt": "lt",
                "lte": "lte",
            }
            expression = parse_model(
                ValueExpression,
                {
                    "kind": "compare",
                    "operator": mapping[str(operator["operation"])],
                    "comparison_type": operator["type"],
                    "terms": [
                        _value(condition.get(k), context).model_dump(mode="json")
                        for k in ["leftValue", "rightValue"]
                    ],
                },
            )
            expressions["/conditions/conditions/0"] = expression
            branches = [
                BranchCondition(
                    output_index=index,
                    predicate=expression,
                    when=index == 0,
                    source=SourceLocation(
                        artifact_id=node.source.artifact_id,
                        json_pointer=node.source.json_pointer
                        + "/parameters/conditions/conditions/0",
                    ),
                )
                for index in (0, 1)
            ]
            writes = ["$json.*:unchanged-item"]
            behavior = _behavior("FILTER", "SUBSET", "PRESERVE", 1, 2)
        elif kind == "merge":
            _only(p, {"mode", "numberInputs", "options"})
            if (
                p.get("mode", "append") != "append"
                or p.get("numberInputs", 2) != 2
                or p.get("options", {}) != {}
            ):
                raise UnsupportedNode("MERGE_APPEND_TWO_INPUTS_REQUIRED")
            writes = ["$json.*:unchanged-item"]
            behavior = _behavior("APPEND", "SUM_INPUTS", "INPUT_INDEX", 2, 1)
        elif kind == "splitOut":
            _only(p, {"fieldToSplitOut", "include", "options"})
            field = p.get("fieldToSplitOut")
            if (
                not isinstance(field, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", field)
                or p.get("include", "noOtherFields") != "noOtherFields"
                or p.get("options", {}) != {}
            ):
                raise UnsupportedNode("SPLIT_FIELD_NOT_SUPPORTED")
            expressions["/fieldToSplitOut"] = ValueExpression(
                kind="current_field", field_path=field.split(".")
            )
            writes = ["$json.*:array-element"]
            behavior = _behavior("SPLIT_ARRAY", "VARIABLE", "PRESERVE", 1, 1)
        elif kind == "executeWorkflowTrigger":
            _only(p, {"inputSource"})
            if p.get("inputSource") != "passthrough":
                raise UnsupportedNode("SUBWORKFLOW_PASSTHROUGH_REQUIRED")
            writes = ["$json.*:caller-input"]
            behavior = _behavior("SUBWORKFLOW", "ONE_PER_INPUT", "CALLER", 0, 1)
        elif kind == "executeWorkflow":
            _only(p, {"source", "workflowId", "mode", "options", "workflowInputs"})
            identity = fixed_workflow_id(p)
            if (
                identity is None
                or p.get("mode", "once") != "once"
                or p.get("options", {}) not in ({}, {"waitForSubWorkflow": True})
                or p.get("workflowInputs", {}) not in ({}, {"mappingMode": "passThrough"})
            ):
                raise UnsupportedNode("FIXED_SUBWORKFLOW_ALL_ITEMS_REQUIRED")
            if context.dependencies is None:
                raise UnsupportedNode("DEPENDENCY_MANIFEST_NOT_SUPPLIED")
            matched = [
                d
                for d in dependency_closure(context.dependencies, context.side)
                if d.workflow_id == identity
            ]
            if len(matched) != 1:
                raise UnsupportedNode("FIXED_SUBWORKFLOW_NOT_SUPPLIED")
            dependencies = [matched[0].dependency_id]
            effects = [f"call:workflow:{identity}"]
            behavior = _behavior("SUBWORKFLOW", "VARIABLE", "CALLER", 1, 1)
        elif kind in {"webhook", "httpRequest", "respondToWebhook"}:
            from flowdelta.http_adapters import describe_http

            expressions, writes, effects, capabilities, behavior = describe_http(
                node.model_copy(update={"parameters": p}), context
            )
        else:
            raise UnsupportedNode("NODE_TYPE_VERSION_NOT_SUPPORTED")
        error_behavior = _settings(node, behavior.output_ports)
        if error_behavior.on_error != "stopWorkflow" and kind not in {"set", "httpRequest"}:
            raise UnsupportedNode("ERROR_MODE_NOT_SUPPORTED_FOR_NODE", "/onError")
        return result.model_copy(
            update={
                "support": "SUPPORTED",
                "resolved_defaults": resolved_defaults,
                "operation": node.parameters.get(
                    "operation", "call_workflow" if kind == "executeWorkflow" else None
                ),
                "reason": "SUPPORTED_DECLARED_CORE_SUBSET",
                "reads": sorted({read for value in expressions.values() for read in _reads(value)}),
                "writes": writes,
                "effects": effects,
                "capabilities": capabilities,
                "expressions": expressions,
                "assignments": assignments_result,
                "branches": branches,
                "item_behavior": behavior,
                "error_behavior": error_behavior,
                "dependency_ids": dependencies,
                "parameter_sources": [
                    SourceLocation(
                        artifact_id=node.source.artifact_id,
                        json_pointer=node.source.json_pointer + "/parameters" + pointer,
                    )
                    for pointer in expressions
                ],
            }
        )
    except (UnsupportedNode, UnsupportedExpression, InputError, ValidationError, TypeError) as exc:
        reason = (
            exc.reason
            if isinstance(exc, UnsupportedNode)
            else "PARAMETER_OR_EXPRESSION_UNSUPPORTED"
        )
        pointer = exc.path if isinstance(exc, UnsupportedNode) else "/parameters"
        return result.model_copy(
            update={
                "reason": reason,
                "unsupported_locations": [
                    SourceLocation(
                        artifact_id=node.source.artifact_id,
                        json_pointer=node.source.json_pointer + pointer,
                    )
                ],
            }
        )


def describe_core(workflow: NormalizedWorkflow, context: AdapterContext) -> list[NodeSemantics]:
    settings = workflow.opaque_fields.get("settings", {})
    allowed_settings = {
        "executionOrder",
        "saveDataErrorExecution",
        "saveDataSuccessExecution",
        "saveExecutionProgress",
        "saveManualExecutions",
    }
    unsupported = (
        not isinstance(settings, dict)
        or settings.get("executionOrder") != "v1"
        or bool(set(settings) - allowed_settings)
        or bool(set(workflow.opaque_fields) - {"id", "name", "active", "settings"})
    )
    results = [describe_core_node(node, context) for node in workflow.nodes]
    by_id = {item.node_id: item for item in results}
    invalid_ports = False
    for edge in workflow.connections:
        source, target = by_id[edge.source_node_id], by_id[edge.target_node_id]
        if edge.source_output_type != "main" or edge.target_input_type != "main":
            invalid_ports = True
        if source.item_behavior:
            outputs = source.item_behavior.output_ports
            if source.error_behavior and source.error_behavior.on_error == "continueErrorOutput":
                outputs += 1
            invalid_ports |= edge.source_output_index >= outputs
        if target.item_behavior:
            invalid_ports |= edge.target_input_index >= target.item_behavior.input_ports
    if unsupported or invalid_ports:
        return [
            NodeSemantics(
                node_id=node.node_id,
                side=context.side,
                node_type=node.node_type,
                type_version=node.type_version,
                operation=node.parameters.get("operation"),
                source=node.source,
                support="UNSUPPORTED",
                reason="WORKFLOW_SETTINGS_NOT_SUPPORTED"
                if unsupported
                else "CONNECTION_PORT_NOT_SUPPORTED",
                unsupported_locations=[
                    SourceLocation(artifact_id=node.source.artifact_id, json_pointer="/settings")
                ],
            )
            for node in workflow.nodes
        ]
    return results
