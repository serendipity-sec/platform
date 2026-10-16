"""Static impact paths from changed nodes to output nodes; a possibility, not an observed effect."""

from collections import deque
from typing import Literal

from flowdelta.contracts import NodeSemantics
from flowdelta.graph.control import short_hash
from flowdelta.pipeline_contracts import Change, GraphEdge, ImpactPath, SemanticGraph

Side = Literal["before", "after"]
_FOLLOWED = ("CONTROL", "DATA")
# Trigger effects are inputs; a node that only receives is not where a change comes out.
_INPUT_EFFECT = "receive:"

POLICY_NOTE = (
    "정책 규칙은 자연어 condition·resource 문자열이라 노드와 연결할 구조화 근거가 없어 "
    "policy_rule_ids를 비워 두었다."
)


def _known(facts: dict[str, NodeSemantics], node_id: str) -> bool:
    return node_id in facts and facts[node_id].support == "SUPPORTED"


def output_nodes(graph: SemanticGraph, facts: dict[str, NodeSemantics]) -> set[str]:
    """Nodes with a non-input effect or unknown effects, plus last nodes (the workflow result)."""
    has_next = {edge.source_node_id for edge in graph.edges if edge.relation == "CONTROL"}
    outputs: set[str] = set()
    for node_id in graph.node_ids:
        # Unknown effects are kept as outputs so a node that may act is never silently skipped.
        effects = facts[node_id].effects if _known(facts, node_id) else None
        if (
            node_id not in has_next
            or effects is None
            or any(not effect.startswith(_INPUT_EFFECT) for effect in effects)
        ):
            outputs.add(node_id)
    return outputs


def shortest_paths(graph: SemanticGraph, start: str) -> dict[str, list[GraphEdge]]:
    """BFS over CONTROL and DATA edges in edge-ID order, so ties resolve the same way each run."""
    following: dict[str, list[GraphEdge]] = {}
    for edge in sorted(graph.edges, key=lambda item: item.edge_id):
        if edge.relation in _FOLLOWED:
            following.setdefault(edge.source_node_id, []).append(edge)
    paths: dict[str, list[GraphEdge]] = {start: []}
    queue = deque([start])
    while queue:
        node_id = queue.popleft()
        for edge in following.get(node_id, []):
            if edge.target_node_id not in paths:
                paths[edge.target_node_id] = [*paths[node_id], edge]
                queue.append(edge.target_node_id)
    return paths


def _side(change: Change) -> Side:
    # Removed nodes and removed connections only exist in the before workflow.
    removed_connection = change.kind == "CONNECTION_CHANGED" and change.after_source is None
    return "before" if change.kind == "REMOVED" or removed_connection else "after"


def _routes(
    change: Change, side: Side, graph: SemanticGraph
) -> tuple[str, dict[str, list[GraphEdge]]] | None:
    """Start node and its shortest paths; a connection change must go over that connection."""
    if change.node_id is None or change.node_id not in graph.node_ids:
        return None
    if change.kind != "CONNECTION_CHANGED":
        return change.node_id, shortest_paths(graph, change.node_id)
    location = change.before_source if side == "before" else change.after_source
    edge = next(
        (item for item in graph.edges if item.relation == "CONTROL" and location in item.evidence),
        None,
    )
    if edge is None:
        return None
    onward = shortest_paths(graph, edge.target_node_id)
    return edge.source_node_id, {end: [edge, *path] for end, path in onward.items()}


def _path(
    side: Side,
    start: str,
    edges: list[GraphEdge],
    change_ids: list[str],
    facts: dict[str, NodeSemantics],
    task_id: str,
) -> ImpactPath:
    node_ids = [start, *(edge.target_node_id for edge in edges)]
    unsupported = [node_id for node_id in node_ids if not _known(facts, node_id)]
    limitations = [POLICY_NOTE]
    if not _known(facts, start):
        support: Literal["SUPPORTED", "PARTIAL", "UNKNOWN"] = "UNKNOWN"
        limitations.append(
            f"시작 노드 {start}의 의미 정보가 없거나 지원되지 않아 지원 범위를 판단하지 않았다."
        )
    elif unsupported:
        support = "PARTIAL"
        limitations.append(
            f"경로 위 {', '.join(unsupported)}는 의미 정보가 없거나 지원되지 않아 "
            "효과를 확인하지 못했다."
        )
    else:
        support = "SUPPORTED"
    calls = [node_id for node_id in node_ids if node_id in facts and facts[node_id].dependency_ids]
    if calls:
        limitations.append(
            f"하위 workflow 호출 노드 {', '.join(calls)}에서 하위 workflow 안으로는 "
            "따라가지 않았다."
        )
    edge_ids = [edge.edge_id for edge in edges]
    return ImpactPath(
        path_id="impact-" + short_hash(side, node_ids[-1], *edge_ids),
        side=side,
        change_ids=change_ids,
        edge_ids=edge_ids,
        task_ids=[task_id],
        policy_rule_ids=[],
        support=support,
        limitations=limitations,
    )


def impact_paths(
    changes: list[Change],
    graphs: dict[Side, SemanticGraph],
    facts: dict[Side, dict[str, NodeSemantics]],
    task_id: str,
) -> tuple[list[ImpactPath], int]:
    """One shortest path per (change, output node); the int counts changes with no start node."""
    outputs = {side: output_nodes(graphs[side], facts[side]) for side in graphs}
    found: dict[tuple[Side, str, tuple[str, ...]], tuple[str, list[GraphEdge], set[str]]] = {}
    unplaced = 0
    for change in changes:
        side = _side(change)
        routes = _routes(change, side, graphs[side])
        if routes is None:
            unplaced += 1
            continue
        start, reachable = routes
        for end in sorted(outputs[side] & reachable.keys()):
            edges = reachable[end]
            key = (side, end, tuple(edge.edge_id for edge in edges))
            _, _, change_ids = found.setdefault(key, (start, edges, set()))
            change_ids.add(change.change_id)
    return [
        _path(side, start, edges, sorted(change_ids), facts[side], task_id)
        for (side, _, _), (start, edges, change_ids) in sorted(found.items())
    ], unplaced
