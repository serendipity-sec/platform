"""Resolve only explicitly supplied local dependency files, with bounded graph traversal."""

from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import DependencyManifest, DependencySpec
from flowdelta.ingestion import InputError, json_object


def dependency_closure(
    manifest: DependencyManifest, side: Literal["before", "after"]
) -> list[DependencySpec]:
    by_id = {item.dependency_id: item for item in manifest.dependencies}
    visited: set[str] = set()
    active: set[str] = set()

    def visit(key: str) -> None:
        if key not in by_id:
            raise InputError("DEPENDENCY_NOT_DECLARED", location="/dependencies")
        if key in active:
            raise InputError("DEPENDENCY_CYCLE", location="/dependencies")
        if key in visited:
            return
        active.add(key)
        for child in by_id[key].requires:
            visit(child)
        active.remove(key)
        visited.add(key)

    for root in manifest.before_roots if side == "before" else manifest.after_roots:
        visit(root)
    return [by_id[key] for key in sorted(visited)]


def validate_manifest(manifest: DependencyManifest) -> None:
    closures = [dependency_closure(manifest, "before"), dependency_closure(manifest, "after")]
    for closure in closures:
        ids = [item.workflow_id for item in closure if item.workflow_id is not None]
        if len(set(ids)) != len(ids):
            raise InputError("AMBIGUOUS_DEPENDENCY_WORKFLOW_ID")
    reached = {item.dependency_id for group in closures for item in group}
    if reached != {item.dependency_id for item in manifest.dependencies}:
        raise InputError("DEPENDENCY_UNREACHABLE", location="/dependencies")


def fixed_workflow_id(parameters: dict[str, JsonValue]) -> str | None:
    """Understand a literal database id, never a path, expression, or remote reference."""
    if parameters.get("source", "database") != "database":
        return None
    value = parameters.get("workflowId")
    if isinstance(value, dict):
        if value.get("mode") != "id":
            return None
        value = value.get("value")
    if not isinstance(value, str) or not value or value.startswith("="):
        return None
    return value


def workflow_dependency_limits(
    document: dict[str, JsonValue], supplied_workflow_ids: set[str]
) -> list[str]:
    limits: list[str] = []
    settings = json_object(document.get("settings", {}))
    error_workflow = settings.get("errorWorkflow")
    if error_workflow:
        if not isinstance(error_workflow, str) or error_workflow.startswith("="):
            limits.append("DYNAMIC_ERROR_WORKFLOW_UNSUPPORTED")
        elif error_workflow not in supplied_workflow_ids:
            limits.append("ERROR_WORKFLOW_NOT_SUPPLIED")
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise InputError("INVALID_NODE_LIST")
    for raw in nodes:
        node = json_object(raw)
        node_type = node.get("type")
        if isinstance(node_type, str) and node_type in {
            "n8n-nodes-base.executeWorkflow",
            "@n8n/n8n-nodes-langchain.toolWorkflow",
        }:
            identity = fixed_workflow_id(json_object(node.get("parameters", {})))
            if identity is None:
                limits.append("DYNAMIC_OR_FILE_SUBWORKFLOW_UNSUPPORTED")
            elif identity not in supplied_workflow_ids:
                limits.append("FIXED_SUBWORKFLOW_NOT_SUPPLIED")
    return sorted(set(limits))
