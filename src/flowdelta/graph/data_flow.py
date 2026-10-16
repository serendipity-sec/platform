"""$json field-flow DATA edges from adapter reads/writes; a static possibility, not a run."""

from collections import deque
from typing import Literal, TypeGuard

from flowdelta.contracts import NodeSemantics, NormalizedWorkflow
from flowdelta.graph.control import DATA_ORIGIN, edge_id, location_text
from flowdelta.pipeline_contracts import GraphEdge, SemanticGraph

PASS_THROUGH = "$json.*:unchanged-item"
# Set with "Include Other Fields": fields the node does not write itself pass on unchanged.
PRESERVED_INPUT = "$json.*:preserved-input"
_PASSES_ON = (PASS_THROUGH, PRESERVED_INPUT)
# Writers that emit whole items whose field names the adapter cannot list.
WILDCARD_WRITES = ("$json.*:array-element", "$json.*:caller-input")
_JSON = "$json."
# Named reads belong to name-reference edges; http.* is the outside request, not a node.
_HANDLED_ELSEWHERE = ("$(", "http.")

Stop = tuple[
    str | None,
    Literal["NO_UPSTREAM", "UNUSABLE", "NOT_PASSED", "NOT_FOUND", "ERROR_OUTPUT", "LOOP"],
]


def usable(facts: NodeSemantics | None) -> TypeGuard[NodeSemantics]:
    return (
        facts is not None
        and facts.support == "SUPPORTED"
        and facts.reads is not None
        and facts.writes is not None
    )


def _matches(written: str, read: str) -> bool:
    return written == read or read.startswith(written + ".") or written.startswith(read + ".")


def _supplied_by(facts: NodeSemantics, read: str) -> list[str]:
    return [
        written
        for written in facts.writes or []
        if written in WILDCARD_WRITES or _matches(written, read)
    ]


def _parents(graph: SemanticGraph) -> dict[str, list[tuple[str, int]]]:
    """(parent, output port) per node; the port tells an error output from a regular one."""
    parents: dict[str, set[tuple[str, int]]] = {}
    for edge in graph.edges:
        if edge.relation == "CONTROL":
            parents.setdefault(edge.target_node_id, set()).add(
                (edge.source_node_id, edge.output_port)
            )
    return {node_id: sorted(items) for node_id, items in parents.items()}


def _error_port(facts: NodeSemantics, port: int) -> bool:
    error = facts.error_behavior
    return error is not None and error.error_output_index == port


def _trace(
    reader: str,
    read: str,
    parents: dict[str, list[tuple[str, int]]],
    facts: dict[str, NodeSemantics],
) -> tuple[list[tuple[str, str]], list[Stop]]:
    """Walk CONTROL edges backwards on every branch; cross only declared pass-through nodes."""
    if not parents.get(reader):
        return [], [(None, "NO_UPSTREAM")]
    hits: list[tuple[str, str]] = []
    stops: list[Stop] = []
    seen = {reader}
    queue = deque(parents[reader])
    while queue:
        node_id, port = queue.popleft()
        if node_id == reader and (reader, "LOOP") not in stops:
            stops.append((reader, "LOOP"))
        node_facts = facts.get(node_id)
        if usable(node_facts) and _error_port(node_facts, port):
            # Declared writes describe the regular output; the error item is not modelled.
            # Not marked seen, so the same node's regular output can still be followed.
            if (node_id, "ERROR_OUTPUT") not in stops:
                stops.append((node_id, "ERROR_OUTPUT"))
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        if not usable(node_facts):
            stops.append((node_id, "UNUSABLE"))
            continue
        written = _supplied_by(node_facts, read)
        if written:
            hits.extend((node_id, item) for item in written)
        elif not any(item in _PASSES_ON for item in node_facts.writes or []):
            stops.append((node_id, "NOT_PASSED"))
        elif not parents.get(node_id):
            stops.append((node_id, "NO_UPSTREAM"))
        else:
            queue.extend(parents[node_id])
    if not hits and not stops:
        stops.append((None, "NOT_FOUND"))
    return hits, stops


def _stop_text(where: str, reader: str, read: str, stop: Stop) -> str:
    node_id, reason = stop
    prefix = f"{where}: {reader}가 읽는 {read}"
    if reason == "UNUSABLE":
        return f"{prefix}의 출처를 찾다가 의미 정보가 없거나 지원되지 않는 {node_id}에서 멈췄다."
    if reason == "NOT_PASSED":
        return (
            f"{prefix}를 {node_id}가 쓰지 않고, 값을 그대로 넘긴다는 선언도 없어 "
            "더 따라가지 않았다."
        )
    if reason == "NOT_FOUND":
        return f"{prefix}를 쓰는 앞 노드를 찾지 못했다."
    if reason == "ERROR_OUTPUT":
        return (
            f"{prefix}의 출처를 찾다가 {node_id}의 오류 출력으로 이어져, "
            "정상 출력의 쓰기 선언을 적용하지 않았다."
        )
    if reason == "LOOP":
        return (
            f"{prefix}의 출처를 찾다가 순환으로 {node_id} 자신에게 돌아와 "
            "그 갈래는 따라가지 않았다."
        )
    if node_id is not None:
        return f"{prefix}의 출처를 찾다가 {node_id}보다 앞 노드가 없어 멈췄다."
    return f"{prefix}를 쓰는 앞 노드가 없다."


def add_field_flow_edges(
    graph: SemanticGraph, workflow: NormalizedWorkflow, facts: dict[str, NodeSemantics]
) -> SemanticGraph:
    """DATA edge: writer -> reader, one per node pair, fields in source_fields/target_fields."""
    nodes = {node.node_id: node for node in workflow.nodes}
    parents = _parents(graph)
    found: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    limitations = list(graph.limitations)
    for node in workflow.nodes:
        where = location_text(node.source)
        reader = facts.get(node.node_id)
        if not usable(reader):
            limitations.append(
                f"{where}의 {node.node_id}는 의미 정보가 없거나 지원되지 않아 "
                "필드 흐름을 계산하지 않았다."
            )
            continue
        for read in reader.reads or []:
            if read.startswith(_HANDLED_ELSEWHERE):
                continue
            if not read.startswith(_JSON):
                limitations.append(
                    f"{where}의 읽기 {read}는 해석하지 않는 형식이라 연결하지 않았다."
                )
                continue
            hits, stops = _trace(node.node_id, read, parents, facts)
            for writer, written in hits:
                sources, targets = found.setdefault((writer, node.node_id), (set(), set()))
                sources.add(written)
                targets.add(read)
            limitations.extend(_stop_text(where, node.node_id, read, stop) for stop in stops)
    edges = list(graph.edges)
    for (writer, reader_id), (sources, targets) in sorted(found.items()):
        target_fields = sorted(targets)
        edges.append(
            GraphEdge(
                edge_id=edge_id(
                    "data", writer, reader_id, ("field", writer, reader_id, *target_fields)
                ),
                source_node_id=writer,
                target_node_id=reader_id,
                relation="DATA",
                output_port=0,
                input_port=0,
                source_fields=sorted(sources),
                target_fields=target_fields,
                adapter_version=facts[reader_id].adapter_version,
                evidence=[nodes[writer].source, nodes[reader_id].source],
                basis="STATIC_POSSIBILITY",
                extensions={DATA_ORIGIN: "FIELD_FLOW"},
            )
        )
    return SemanticGraph(
        node_ids=graph.node_ids, edges=edges, limitations=limitations, nodes=graph.nodes
    )
