"""Control edges from normalized connections; a static possibility, never an execution record."""

import hashlib
import re

from flowdelta.contracts import Connection, NodeSemantics, NormalizedWorkflow, SourceLocation
from flowdelta.ingestion import InputError
from flowdelta.pipeline_contracts import GraphEdge, SemanticGraph

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")

# DATA edges come from two rules; consumers tell them apart by this extension.
DATA_ORIGIN = "flowdelta.graph.data_origin"

GRAPH_LIMITATIONS = [
    "CONTROL 관계와 DATA 관계(이름 참조·$json 필드 흐름)만 계산했다. "
    "지시 영향·권한 관계는 아직 계산하지 않았다.",
    "정적 연결 가능성이며 실제 실행 경로를 관측한 자료가 아니다.",
]


def short_hash(*parts: object, length: int = 16) -> str:
    joined = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def edge_id(prefix: str, source: str, target: str, key: tuple[object, ...]) -> str:
    """Same relation gets the same ID on both sides; the hash keeps dashed IDs unambiguous."""
    readable = f"{prefix}-{source}-{target}-{short_hash(*key, length=8)}"
    if _IDENTIFIER.match(readable) and len(readable) <= 128:
        return readable
    return f"{prefix}-{short_hash(*key)}"


def control_edge_id(connection: Connection) -> str:
    key = (
        connection.source_node_id,
        connection.source_output_type,
        connection.source_output_index,
        connection.target_node_id,
        connection.target_input_type,
        connection.target_input_index,
    )
    return edge_id("control", connection.source_node_id, connection.target_node_id, key)


def location_text(source: SourceLocation) -> str:
    return f"{source.artifact_id}#{source.json_pointer}"


def build_control_graph(
    workflow: NormalizedWorkflow, facts: dict[str, NodeSemantics]
) -> SemanticGraph:
    node_ids = [node.node_id for node in workflow.nodes]
    duplicates = {node.node_id for node in workflow.nodes if node_ids.count(node.node_id) > 1}
    if duplicates:
        first = next(node for node in workflow.nodes if node.node_id in duplicates)
        raise InputError("GRAPH_DUPLICATE_NODE_ID", location=location_text(first.source))
    known = set(node_ids)
    edges: list[GraphEdge] = []
    for connection in workflow.connections:
        if connection.source_node_id not in known or connection.target_node_id not in known:
            raise InputError("GRAPH_DANGLING_CONNECTION", location=location_text(connection.source))
        source_facts = facts.get(connection.source_node_id)
        branch = None
        if source_facts is not None:
            branch = next(
                (
                    item
                    for item in source_facts.branches
                    if (item.output_type, item.output_index)
                    == (connection.source_output_type, connection.source_output_index)
                ),
                None,
            )
        edges.append(
            GraphEdge(
                edge_id=control_edge_id(connection),
                source_node_id=connection.source_node_id,
                target_node_id=connection.target_node_id,
                relation="CONTROL",
                output_port=connection.source_output_index,
                input_port=connection.target_input_index,
                output_type=connection.source_output_type,
                input_type=connection.target_input_type,
                condition=branch,
                adapter_version=source_facts.adapter_version if branch and source_facts else None,
                evidence=[connection.source],
                basis="STATIC_POSSIBILITY",
            )
        )
    if len({edge.edge_id for edge in edges}) != len(edges):
        raise InputError("GRAPH_DUPLICATE_CONNECTION", location=workflow.source_artifact_id)
    limitations = list(GRAPH_LIMITATIONS)
    if workflow.semantic_support != "SUPPORTED":
        limitations.append(f"정규화 workflow의 의미 지원 상태: {workflow.semantic_support}.")
    return SemanticGraph(node_ids=node_ids, edges=edges, limitations=limitations)
