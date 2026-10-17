"""Static impact paths from changed nodes to output nodes; a possibility, not an observed effect."""

from collections import deque
from typing import Literal, NamedTuple

from flowdelta.contracts import NodeSemantics
from flowdelta.graph.control import DATA_ORIGIN, short_hash
from flowdelta.graph.references import EXTENSION as NAME_REFERENCES
from flowdelta.pipeline_contracts import Change, GraphEdge, ImpactPath, SemanticGraph

Side = Literal["before", "after"]
# (side, start node, shortest path per reachable node, note for the path's limitations)
Route = tuple[Side, str, dict[str, list[GraphEdge]], str | None]
# Rename verdicts where some references still call the old name after the change.
_STALE = ("BROKEN_REFERENCES", "REFERENCES_RETARGETED")
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
    return edge.source_node_id, _onward(graph, edge)


def _onward(graph: SemanticGraph, edge: GraphEdge) -> dict[str, list[GraphEdge]]:
    return {end: [edge, *path] for end, path in shortest_paths(graph, edge.target_node_id).items()}


def _stale_references(change: Change) -> set[tuple[str, str]]:
    """(consumer, pointer) of after-side references that still call the old name."""
    verdict = change.extensions.get(NAME_REFERENCES)
    if not isinstance(verdict, dict) or verdict.get("status") not in _STALE:
        return set()
    references = verdict.get("references")
    stale: set[tuple[str, str]] = set()
    for item in references if isinstance(references, list) else []:
        if not isinstance(item, dict):
            continue
        if (item.get("side"), item.get("name")) != ("after", change.before_value):
            continue
        node_id, pointer = item.get("node_id"), item.get("pointer")
        if isinstance(node_id, str) and isinstance(pointer, str):
            stale.add((node_id, pointer))
    return stale


def _reference_routes(change: Change, graphs: dict[Side, SemanticGraph]) -> list[Route]:
    """A rename left references on the old name: follow the lost and the retargeted relation."""
    stale = _stale_references(change)
    consumers = {node_id for node_id, _ in stale}
    routes: list[Route] = []
    for edge in graphs["before"].edges:
        if (
            edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE"
            and edge.source_node_id == change.node_id
            and edge.target_node_id in consumers
        ):
            note = (
                f"이름 변경 뒤 끊긴 이름 참조 {edge.source_node_id} → {edge.target_node_id}를 "
                "수정 전 그래프에서 따라갔다."
            )
            routes.append(("before", edge.source_node_id, _onward(graphs["before"], edge), note))
    for edge in graphs["after"].edges:
        if edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE" and any(
            (edge.target_node_id, item.json_pointer) in stale for item in edge.evidence
        ):
            note = (
                f"옛 이름을 가져간 {edge.source_node_id}로 바뀐 이름 참조 "
                f"{edge.source_node_id} → {edge.target_node_id}에서 출발했다."
            )
            routes.append(("after", edge.source_node_id, _onward(graphs["after"], edge), note))
    return routes


def _path(
    side: Side,
    start: str,
    edges: list[GraphEdge],
    change_ids: list[str],
    notes: list[str],
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
    limitations.extend(notes)
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


class ImpactResult(NamedTuple):
    paths: list[ImpactPath]
    # Changes with no start node (workflow-level, or a connection no edge matches).
    unplaced: int
    # DISPLAY_CHANGED: position, notes, colour, workflow name or tags; no execution meaning.
    display_only: int


def impact_paths(
    changes: list[Change],
    graphs: dict[Side, SemanticGraph],
    facts: dict[Side, dict[str, NodeSemantics]],
    task_id: str,
) -> ImpactResult:
    """One shortest path per (change, route, output node), grouped when the edges match."""
    outputs = {side: output_nodes(graphs[side], facts[side]) for side in graphs}
    found: dict[
        tuple[Side, str, tuple[str, ...]], tuple[str, list[GraphEdge], set[str], set[str]]
    ] = {}
    unplaced = display_only = 0
    for change in changes:
        if change.kind == "DISPLAY_CHANGED":
            display_only += 1
            continue
        side = _side(change)
        own = _routes(change, side, graphs[side])
        if own is None:
            unplaced += 1
            continue
        start, reachable = own
        routes: list[Route] = [(side, start, reachable, None), *_reference_routes(change, graphs)]
        for route_side, route_start, route_reachable, note in routes:
            for end in sorted(outputs[route_side] & route_reachable.keys()):
                edges = route_reachable[end]
                key = (route_side, end, tuple(edge.edge_id for edge in edges))
                _, _, change_ids, notes = found.setdefault(key, (route_start, edges, set(), set()))
                change_ids.add(change.change_id)
                if note is not None:
                    notes.add(note)
    paths = [
        _path(side, start, edges, sorted(change_ids), sorted(notes), facts[side], task_id)
        for (side, _, _), (start, edges, change_ids, notes) in sorted(found.items())
    ]
    return ImpactResult(paths, unplaced, display_only)
