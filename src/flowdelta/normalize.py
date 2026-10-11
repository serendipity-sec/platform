"""Lossless structural reading; this module makes no n8n semantic support claims."""

from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import (
    Connection,
    NodeSemantics,
    NormalizedNode,
    NormalizedWorkflow,
    SourceLocation,
)
from flowdelta.ingestion import InputError, digest, json_bytes, json_object, parse_model

MAX_NODES = 1000
MAX_CONNECTIONS = 10000


def _pointer(text: str) -> str:
    return text.replace("~", "~0").replace("/", "~1")


def _port_index(value: JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise InputError("INVALID_PORT_INDEX")
    return value


def normalize(document: dict[str, JsonValue], artifact_id: str) -> NormalizedWorkflow:
    raw_nodes = document.get("nodes")
    raw_connections = document.get("connections")
    if not isinstance(raw_nodes, list) or not raw_nodes or len(raw_nodes) > MAX_NODES:
        raise InputError("INVALID_NODE_LIST")
    if not isinstance(raw_connections, dict):
        raise InputError("CONNECTIONS_REQUIRED")
    nodes: list[NormalizedNode] = []
    names: dict[str, str] = {}
    ids: set[str] = set()
    for index, value in enumerate(raw_nodes):
        raw = json_object(value)
        node = parse_model(
            NormalizedNode,
            {
                "node_id": raw.get("id"),
                "name": raw.get("name"),
                "node_type": raw.get("type"),
                "type_version": raw.get("typeVersion"),
                "parameters": raw.get("parameters"),
                "opaque_fields": {
                    key: item
                    for key, item in raw.items()
                    if key not in {"id", "name", "type", "typeVersion", "parameters"}
                },
                "source": {"artifact_id": artifact_id, "json_pointer": f"/nodes/{index}"},
            },
        )
        if node.node_id in ids or node.name in names:
            raise InputError("AMBIGUOUS_NODE_ID_OR_NAME")
        ids.add(node.node_id)
        names[node.name] = node.node_id
        nodes.append(node)

    connections: list[Connection] = []
    for source_name, raw_outputs in raw_connections.items():
        if source_name not in names:
            raise InputError("UNKNOWN_CONNECTION_SOURCE")
        for output_type, outputs in json_object(raw_outputs).items():
            if not output_type or not isinstance(outputs, list):
                raise InputError("INVALID_OUTPUT_PORTS")
            for output_index, entries in enumerate(outputs):
                if not isinstance(entries, list):
                    raise InputError("INVALID_OUTPUT_PORTS")
                for edge_index, entry in enumerate(entries):
                    raw = json_object(entry)
                    target_name = raw.get("node")
                    input_type = raw.get("type")
                    if not isinstance(target_name, str) or target_name not in names:
                        raise InputError("UNKNOWN_CONNECTION_TARGET")
                    if not isinstance(input_type, str) or not input_type:
                        raise InputError("INVALID_INPUT_PORT_TYPE")
                    if len(connections) >= MAX_CONNECTIONS:
                        raise InputError("CONNECTION_COUNT_LIMIT")
                    connections.append(
                        Connection(
                            source_node_id=names[source_name],
                            source_output_type=output_type,
                            source_output_index=output_index,
                            target_node_id=names[target_name],
                            target_input_type=input_type,
                            target_input_index=_port_index(raw.get("index")),
                            opaque_fields={
                                key: item
                                for key, item in raw.items()
                                if key not in {"node", "type", "index"}
                            },
                            source=SourceLocation(
                                artifact_id=artifact_id,
                                json_pointer=(
                                    f"/connections/{_pointer(source_name)}/"
                                    f"{_pointer(output_type)}/{output_index}/{edge_index}"
                                ),
                            ),
                        )
                    )
    workflow_id = document.get("id")
    if workflow_id is not None and not isinstance(workflow_id, str):
        raise InputError("INVALID_WORKFLOW_ID")
    return NormalizedWorkflow(
        source_artifact_id=artifact_id,
        workflow_id=workflow_id,
        nodes=nodes,
        connections=connections,
        opaque_fields={
            key: item for key, item in document.items() if key not in {"nodes", "connections"}
        },
        original_connections=raw_connections,
        limitations=[
            "Node behavior and expressions have not been interpreted or executed.",
            "Dependency references are preserved as opaque input, not resolved.",
            "Port structure is preserved; compatibility with n8n requires L-03/C-01.",
        ],
    )


def structural_digest(document: dict[str, JsonValue]) -> str:
    # Conservative: keeps coordinates, array order, names, settings and unknown fields.
    # Only JSON object-key order / whitespace are normalized. NOT semantic equivalence.
    return digest(json_bytes({"normalizer_version": "structure-v1", "workflow": document}))


def describe_nodes(
    workflow: NormalizedWorkflow, side: Literal["before", "after"]
) -> list[NodeSemantics]:
    return [
        NodeSemantics(
            node_id=node.node_id,
            side=side,
            node_type=node.node_type,
            type_version=node.type_version,
            operation=node.parameters.get("operation"),
            source=node.source,
        )
        for node in workflow.nodes
    ]
