"""Static impact paths from changed nodes to output nodes; a possibility, not an observed effect."""

import heapq
from collections import deque
from collections.abc import Iterator
from typing import Literal, NamedTuple

from flowdelta.contracts import NodeSemantics
from flowdelta.graph.control import DATA_ORIGIN, short_hash
from flowdelta.graph.references import EXTENSION as NAME_REFERENCES
from flowdelta.pipeline_contracts import Change, GraphEdge, ImpactPath, SemanticGraph

Side = Literal["before", "after"]
# One path: its side, end node and edge IDs; changes reaching the same key share the path.
Key = tuple[Side, str, tuple[str, ...]]
# Selection order of a path: fewest edges first, then path ID.
Candidate = tuple[int, str, Key]
RouteKey = tuple[Side, str, str | None]
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


def _following(graph: SemanticGraph) -> dict[str, list[GraphEdge]]:
    following: dict[str, list[GraphEdge]] = {}
    for edge in sorted(graph.edges, key=lambda item: item.edge_id):
        if edge.relation in _FOLLOWED:
            following.setdefault(edge.source_node_id, []).append(edge)
    return following


def _tree(following: dict[str, list[GraphEdge]], start: str) -> dict[str, GraphEdge | None]:
    """BFS tree: each reachable node's last edge on its shortest path, None for the start."""
    tree: dict[str, GraphEdge | None] = {start: None}
    queue = deque([start])
    while queue:
        node_id = queue.popleft()
        for edge in following.get(node_id, []):
            if edge.target_node_id not in tree:
                tree[edge.target_node_id] = edge
                queue.append(edge.target_node_id)
    return tree


def _back(tree: dict[str, GraphEdge | None], end: str) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    edge = tree[end]
    while edge is not None:
        edges.append(edge)
        edge = tree[edge.source_node_id]
    return edges[::-1]


def shortest_paths(graph: SemanticGraph, start: str) -> dict[str, list[GraphEdge]]:
    """BFS over CONTROL and DATA edges in edge-ID order, so ties resolve the same way each run."""
    tree = _tree(_following(graph), start)
    return {end: _back(tree, end) for end in tree}


def _side(change: Change) -> Side:
    # Removed nodes and removed connections only exist in the before workflow.
    removed_connection = change.kind == "CONNECTION_CHANGED" and change.after_source is None
    return "before" if change.kind == "REMOVED" or removed_connection else "after"


class Route(NamedTuple):
    side: Side
    start: str
    # The changed connection or name reference a route goes over first; None starts at the node.
    via: GraphEdge | None
    # Added to the limitations of every path the route gives.
    note: str | None


def _own_route(change: Change, graphs: dict[Side, SemanticGraph]) -> Route | None:
    """Start at the changed node; a connection change must go over that connection."""
    side = _side(change)
    graph = graphs[side]
    if change.node_id is None or change.node_id not in graph.node_ids:
        return None
    if change.kind != "CONNECTION_CHANGED":
        return Route(side, change.node_id, None, None)
    location = change.before_source if side == "before" else change.after_source
    edge = next(
        (item for item in graph.edges if item.relation == "CONTROL" and location in item.evidence),
        None,
    )
    if edge is None:
        return None
    return Route(side, edge.source_node_id, edge, None)


Location = tuple[str, str]


def _in_node(pointer: str) -> str:
    """Pointer inside the node's parameters, so a node moved in the list still matches."""
    index = pointer.find("/parameters")
    return pointer[index:] if index >= 0 else pointer


def _after_references(change: Change) -> tuple[set[Location], set[Location]]:
    """(consumer, in-node pointer) after the rename: still on the old name, and all of them."""
    verdict = change.extensions.get(NAME_REFERENCES)
    if not isinstance(verdict, dict) or verdict.get("status") not in _STALE:
        return set(), set()
    references = verdict.get("references")
    stale: set[Location] = set()
    seen: set[Location] = set()
    for item in references if isinstance(references, list) else []:
        if not isinstance(item, dict) or item.get("side") != "after":
            continue
        node_id, pointer = item.get("node_id"), item.get("pointer")
        if not isinstance(node_id, str) or not isinstance(pointer, str):
            continue
        seen.add((node_id, _in_node(pointer)))
        if item.get("name") == change.before_value:
            stale.add((node_id, _in_node(pointer)))
    return stale, seen


def _location(edge: GraphEdge) -> Location:
    # A name-reference edge carries exactly the referencing string's location.
    return edge.target_node_id, _in_node(edge.evidence[0].json_pointer)


def _reference_routes(change: Change, graphs: dict[Side, SemanticGraph]) -> list[Route]:
    """A rename left references on the old name: follow the lost and the retargeted relation."""
    stale, seen = _after_references(change)
    candidates = [
        edge
        for edge in graphs["before"].edges
        if edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE"
        and edge.source_node_id == change.node_id
    ]
    # Each lost relation is matched by its own reference; one updated in place stays out.
    unmatched = stale - {_location(edge) for edge in candidates}
    moved = {node_id for node_id, _ in unmatched}
    routes: list[Route] = []
    for edge in candidates:
        note = (
            f"이름 변경 뒤 끊긴 이름 참조 {edge.source_node_id} → {edge.target_node_id}를 "
            "수정 전 그래프에서 따라갔다."
        )
        if _location(edge) in stale:
            routes.append(Route("before", edge.source_node_id, edge, note))
        elif edge.target_node_id in moved and _location(edge) not in seen:
            # The stale reference moved (e.g. reordered parameters), so no pointer pairs it.
            note += " 참조 위치가 바뀌어 위치로 대응하지 못한 같은 노드의 참조를 함께 따라갔다."
            routes.append(Route("before", edge.source_node_id, edge, note))
    for edge in graphs["after"].edges:
        if edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE" and _location(edge) in stale:
            note = (
                f"옛 이름을 가져간 {edge.source_node_id}로 바뀐 이름 참조 "
                f"{edge.source_node_id} → {edge.target_node_id}에서 출발했다."
            )
            routes.append(Route("after", edge.source_node_id, edge, note))
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
        path_id=_path_id(side, node_ids[-1], tuple(edge_ids)),
        side=side,
        change_ids=change_ids,
        edge_ids=edge_ids,
        task_ids=[task_id],
        policy_rule_ids=[],
        support=support,
        limitations=limitations,
    )


def _path_id(side: Side, end: str, edge_ids: tuple[str, ...]) -> str:
    return "impact-" + short_hash(side, end, *edge_ids)


class _ShortestTree:
    """A BFS tree expanded one distance layer at a time and shared by routes."""

    def __init__(self, following: dict[str, list[GraphEdge]], start: str) -> None:
        self.following = following
        self.predecessors: dict[str, GraphEdge | None] = {start: None}
        self._layers = [[start]]
        self._exhausted = False

    def layer(self, index: int) -> list[str] | None:
        """Return one BFS layer, expanding only as far as the requested layer."""
        while len(self._layers) <= index and not self._exhausted:
            next_layer: list[str] = []
            for node_id in self._layers[-1]:
                for edge in self.following.get(node_id, []):
                    if edge.target_node_id not in self.predecessors:
                        self.predecessors[edge.target_node_id] = edge
                        next_layer.append(edge.target_node_id)
            if next_layer:
                self._layers.append(next_layer)
            else:
                self._exhausted = True
        return self._layers[index] if index < len(self._layers) else None

    def ensure_node(self, node_id: str) -> bool:
        index = 0
        while node_id not in self.predecessors:
            if self.layer(index) is None:
                return False
            index += 1
        return True

    def reaches(self, outputs: set[str]) -> bool:
        index = 0
        while (nodes := self.layer(index)) is not None:
            if any(node_id in outputs for node_id in nodes):
                return True
            index += 1
        return False


class _RouteOrder:
    """Cache only the candidate layers consumed from one route's sorted stream."""

    def __init__(self, route: Route, tree: _ShortestTree, outputs: set[str]) -> None:
        self.route = route
        self.tree = tree
        self.outputs = outputs
        self._candidate_layers: list[list[Candidate]] = []
        self._exhausted = False

    def layer(self, index: int) -> list[Candidate] | None:
        """Materialize one distance layer so path-ID ties can be sorted deterministically."""
        while len(self._candidate_layers) <= index and not self._exhausted:
            tree_layer = self.tree.layer(len(self._candidate_layers))
            if tree_layer is None:
                self._exhausted = True
                break
            items: list[Candidate] = []
            for end in tree_layer:
                if end not in self.outputs:
                    continue
                path = _back(self.tree.predecessors, end)
                edges = [self.route.via, *path] if self.route.via is not None else path
                edge_ids = tuple(edge.edge_id for edge in edges)
                path_id = _path_id(self.route.side, end, edge_ids)
                items.append((len(edge_ids), path_id, (self.route.side, end, edge_ids)))
            self._candidate_layers.append(sorted(items))
        return self._candidate_layers[index] if index < len(self._candidate_layers) else None

    def candidates(self) -> Iterator[Candidate]:
        index = 0
        while (items := self.layer(index)) is not None:
            yield from items
            index += 1


def _route_key(route: Route) -> RouteKey:
    return (route.side, route.start, route.via.edge_id if route.via is not None else None)


class _Walks:
    """Lazily share shortest-path trees and candidate layers between changes."""

    def __init__(self, graphs: dict[Side, SemanticGraph], outputs: dict[Side, set[str]]) -> None:
        self.outputs = outputs
        self._following = {side: _following(graph) for side, graph in graphs.items()}
        self._trees: dict[tuple[Side, str], _ShortestTree] = {}
        self._orders: dict[RouteKey, _RouteOrder] = {}
        self._reachability: dict[RouteKey, bool] = {}

    def _tree_for(self, route: Route) -> _ShortestTree:
        start = route.via.target_node_id if route.via is not None else route.start
        key = (route.side, start)
        if key not in self._trees:
            self._trees[key] = _ShortestTree(self._following[route.side], start)
        return self._trees[key]

    def order(self, route: Route) -> Iterator[Candidate]:
        """Yield paths by distance and path ID without materializing the full route."""
        key = _route_key(route)
        if key not in self._orders:
            self._orders[key] = _RouteOrder(route, self._tree_for(route), self.outputs[route.side])
        return self._orders[key].candidates()

    def edges(self, route: Route, end: str) -> list[GraphEdge] | None:
        tree = self._tree_for(route)
        if not tree.ensure_node(end):
            return None
        path = _back(tree.predecessors, end)
        return [route.via, *path] if route.via is not None else path

    def reaches_output(self, route: Route) -> bool:
        key = _route_key(route)
        if key not in self._reachability:
            self._reachability[key] = self._tree_for(route).reaches(self.outputs[route.side])
        return self._reachability[key]


def _queue(routes: list[Route], walks: _Walks) -> Iterator[Candidate]:
    """One change's paths in selection order, worked out only once the selection reaches it."""
    last: Candidate | None = None
    for item in heapq.merge(*(walks.order(route) for route in routes)):
        if item != last:
            yield item
        last = item


def _select(
    placed: dict[str, list[Route]], walks: _Walks, limit: int | None
) -> tuple[set[Key], bool]:
    """Round robin over changes in ID order, each taking its shortest path not yet taken.

    Stops consuming streams at the first new path past the limit. The flag says whether every
    path was seen, i.e. the kept paths are all of them.
    """
    queues = [_queue(placed[change_id], walks) for change_id in sorted(placed)]
    kept: set[Key] = set()
    while queues:
        active: list[Iterator[Candidate]] = []
        for queue in queues:
            item = next(queue, None)
            if item is None:
                continue
            active.append(queue)
            if item[2] in kept:
                continue
            if limit is not None and len(kept) == limit:
                return kept, False
            kept.add(item[2])
        queues = active
    return kept, True


class ImpactResult(NamedTuple):
    paths: list[ImpactPath]
    # Changes with no start node (workflow-level, or a connection no edge matches).
    unplaced: int
    # DISPLAY_CHANGED: position, notes, colour, workflow name or tags; no execution meaning.
    display_only: int
    # False when generation stopped at the path limit with paths left, so the total is unknown.
    complete: bool = True
    # Changes that have a path, none of which fits under the limit.
    uncovered: int = 0


def impact_paths(
    changes: list[Change],
    graphs: dict[Side, SemanticGraph],
    facts: dict[Side, dict[str, NodeSemantics]],
    task_id: str,
    limit: int | None = None,
) -> ImpactResult:
    """One shortest path per (change, route, output node), grouped when the edges match.

    With a limit, candidate streams stop once one extra unique path proves truncation. Only the
    selected paths become ImpactPath objects; each requested BFS layer is sorted by path ID.
    """
    if limit is not None and limit < 0:
        raise ValueError("impact path limit must be non-negative")
    walks = _Walks(graphs, {side: output_nodes(graphs[side], facts[side]) for side in graphs})
    placed: dict[str, list[Route]] = {}
    unplaced = display_only = 0
    route_examples: dict[RouteKey, Route] = {}
    route_changes: dict[RouteKey, dict[str, set[str]]] = {}
    for change in changes:
        if change.kind == "DISPLAY_CHANGED":
            display_only += 1
            continue
        own = _own_route(change, graphs)
        if own is None:
            unplaced += 1
            continue
        routes = [own, *_reference_routes(change, graphs)]
        placed[change.change_id] = routes
        for route in routes:
            route_key = _route_key(route)
            route_examples.setdefault(route_key, route)
            notes = route_changes.setdefault(route_key, {}).setdefault(change.change_id, set())
            if route.note is not None:
                notes.add(route.note)
    kept, complete = _select(placed, walks, limit)
    # A kept key can only be shared by its own start route or by a route that uses
    # the key's first edge as its required first edge. Check those route signatures only.
    edges_by_id = {
        side: {edge.edge_id: edge for edge in graph.edges} for side, graph in graphs.items()
    }
    found: dict[Key, tuple[str, list[GraphEdge], set[str], set[str]]] = {}
    covered_changes: set[str] = set()
    for key in kept:
        side, end, edge_ids = key
        if edge_ids:
            first = edges_by_id[side].get(edge_ids[0])
            if first is None:
                continue
            route_keys = [
                (side, first.source_node_id, None),
                (side, first.source_node_id, first.edge_id),
            ]
        else:
            route_keys = [(side, end, None)]
        for route_key in route_keys:
            changes_for_route = route_changes.get(route_key)
            if not changes_for_route:
                continue
            route = route_examples[route_key]
            edges = walks.edges(route, end)
            if edges is None or tuple(edge.edge_id for edge in edges) != edge_ids:
                continue
            _, _, change_ids, notes = found.setdefault(key, (route.start, edges, set(), set()))
            change_ids.update(changes_for_route)
            covered_changes.update(changes_for_route)
            for route_notes in changes_for_route.values():
                notes.update(route_notes)
    uncovered = sum(
        1
        for change_id, routes in placed.items()
        if change_id not in covered_changes and any(walks.reaches_output(route) for route in routes)
    )
    paths = [
        _path(side, start, edges, sorted(change_ids), sorted(notes), facts[side], task_id)
        for (side, _, _), (start, edges, change_ids, notes) in sorted(found.items())
    ]
    return ImpactResult(paths, unplaced, display_only, complete, uncovered)
