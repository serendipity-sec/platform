"""Find the code a static template reads in one workflow, without running anything (#51).

A target is a code node that an AI Agent can call as a tool (`ai_tool` connection). Every other
node connected to an agent as a tool is listed as unsupported with a reason, so a node the
template cannot read is never silently treated as "no finding". The code is returned with its
JSON Pointer and hash in the original workflow, so a finding points back to the exact source.
"""

import json
from dataclasses import dataclass

from flowdelta.ingestion import InputError, digest
from flowdelta.runtime.static_scan import StaticScanTemplate, UnsupportedTarget


@dataclass(frozen=True)
class CodeTarget:
    node_id: str
    node_name: str
    node_type: str
    type_version: str
    code_pointer: str  # JSON Pointer in the original workflow file.
    code: str
    code_sha256: str


@dataclass(frozen=True)
class WorkflowTargets:
    scanned: list[CodeTarget]
    unsupported: list[UnsupportedTarget]


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def find_targets(workflow: bytes, template: StaticScanTemplate) -> WorkflowTargets:
    """Split the agent tools of one workflow into code to scan and unsupported nodes."""
    try:
        document = json.loads(workflow)
        nodes = document["nodes"]
        connections = document.get("connections", {})
        if not isinstance(nodes, list) or not isinstance(connections, dict):
            raise TypeError
        by_name = {str(node["name"]): (index, node) for index, node in enumerate(nodes)}
    except (ValueError, KeyError, TypeError) as exc:
        raise InputError("STATIC_WORKFLOW_INVALID") from exc

    agent_types = {target.agent_node_type for target in template.targets}
    edges: list[tuple[str, str]] = []  # (tool name, agent name)
    try:
        for source, outputs in connections.items():
            for group in outputs.get("ai_tool", []):
                for link in group:
                    edges.append((str(source), str(link["node"])))
    except (AttributeError, KeyError, TypeError) as exc:
        raise InputError("STATIC_WORKFLOW_INVALID") from exc

    scanned: dict[str, CodeTarget] = {}
    unsupported: dict[str, UnsupportedTarget] = {}
    for tool_name, agent_name in sorted(set(edges)):
        if tool_name not in by_name or agent_name not in by_name:
            raise InputError("STATIC_WORKFLOW_INVALID")
        index, tool = by_name[tool_name]
        _, agent = by_name[agent_name]
        if agent.get("type") not in agent_types:
            continue  # Not an agent tool connection this template is about.
        node_id = str(tool.get("id") or tool_name)
        if node_id in scanned or node_id in unsupported:
            continue
        node_type = str(tool.get("type"))
        version = str(tool.get("typeVersion"))
        agent_version = str(agent.get("typeVersion"))
        matches = [
            target
            for target in template.targets
            if target.agent_node_type == agent.get("type")
            and agent_version in target.agent_type_versions
        ]
        if not matches:
            unsupported[node_id] = UnsupportedTarget(
                node_id=node_id, node_type=node_type, reason="AGENT_VERSION_NOT_SUPPORTED"
            )
            continue
        target = next(
            (
                item
                for item in matches
                if item.node_type == node_type and version in item.type_versions
            ),
            None,
        )
        if target is None:
            unsupported[node_id] = UnsupportedTarget(
                node_id=node_id, node_type=node_type, reason="TARGET_TYPE_NOT_SUPPORTED"
            )
            continue
        parameters = tool.get("parameters")
        code = parameters.get(target.code_parameter) if isinstance(parameters, dict) else None
        if not isinstance(code, str) or not code.strip():
            unsupported[node_id] = UnsupportedTarget(
                node_id=node_id, node_type=node_type, reason="CODE_PARAMETER_MISSING"
            )
            continue
        scanned[node_id] = CodeTarget(
            node_id=node_id,
            node_name=tool_name,
            node_type=node_type,
            type_version=version,
            code_pointer=f"/nodes/{index}/parameters/{_pointer_token(target.code_parameter)}",
            code=code,
            code_sha256=digest(code.encode("utf-8")),
        )
    return WorkflowTargets(
        scanned=[scanned[key] for key in sorted(scanned)],
        unsupported=[unsupported[key] for key in sorted(unsupported)],
    )
