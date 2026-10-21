"""Static impact paths from changed nodes to output nodes; a possibility, not an observed effect."""

from collections import deque
from collections.abc import Iterator
from typing import Literal, NamedTuple

from flowdelta.contracts import NodeSemantics
from flowdelta.graph.control import DATA_ORIGIN, is_main, short_hash
from flowdelta.graph.references import EXTENSION as NAME_REFERENCES
from flowdelta.pipeline_contracts import Change, GraphEdge, ImpactPath, SemanticGraph

Side = Literal["before", "after"]
# One path: its side, end node and edge IDs; changes reaching the same key share the path.
Key = tuple[Side, str, tuple[str, ...]]
# A path in selection order: its edge count, then its key.
Candidate = tuple[int, Key]
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


class _Search:
    """BFS from one node in edge-ID order, expanded only as far as callers ask."""

    def __init__(self, following: dict[str, list[GraphEdge]], start: str) -> None:
        self._following = following
        self._queue = deque([start])
        # Each reached node's last edge on its shortest path; None for the start.
        self.tree: dict[str, GraphEdge | None] = {start: None}
        # Reached nodes in discovery order, which is also fewest-edges-first order.
        self.found: list[str] = [start]

    def expand(self) -> bool:
        """Visit the next queued node; False once nothing is left to reach."""
        if not self._queue:
            return False
        node_id = self._queue.popleft()
        for edge in self._following.get(node_id, []):
            if edge.target_node_id not in self.tree:
                self.tree[edge.target_node_id] = edge
                self.found.append(edge.target_node_id)
                self._queue.append(edge.target_node_id)
        return True

    def nth(self, index: int) -> str | None:
        while len(self.found) <= index and self.expand():
            pass
        return self.found[index] if index < len(self.found) else None

    def reaches(self, node_id: str) -> bool:
        while node_id not in self.tree and self.expand():
            pass
        return node_id in self.tree

    def edges(self, end: str) -> list[GraphEdge]:
        edges: list[GraphEdge] = []
        edge = self.tree[end]
        while edge is not None:
            edges.append(edge)
            edge = self.tree[edge.source_node_id]
        return edges[::-1]


def shortest_paths(graph: SemanticGraph, start: str) -> dict[str, list[GraphEdge]]:
    """BFS over CONTROL and DATA edges in edge-ID order, so ties resolve the same way each run."""
    search = _Search(_following(graph), start)
    while search.expand():
        pass
    return {end: search.edges(end) for end in search.found}


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
    sub_node_links = sorted(
        {
            edge.output_type
            for edge in edges
            if edge.relation == "CONTROL" and not is_main(edge.output_type, edge.input_type)
        }
    )
    if sub_node_links:
        limitations.append(
            f"경로의 {', '.join(sub_node_links)} 연결은 실행 순서가 아니라 하위 노드 연결이다. "
            "모델·도구 설정이 agent에 영향을 줄 수 있다는 정적 가능성으로만 따라갔다."
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


def _candidate(route: Route, search: _Search, end: str) -> Candidate:
    edges = search.edges(end)
    if route.via is not None:
        edges = [route.via, *edges]
    edge_ids = tuple(edge.edge_id for edge in edges)
    return len(edge_ids), (route.side, end, edge_ids)


class _Candidates:
    """One route's paths to output nodes, fewest edges first and then in BFS order.

    Built one at a time as the selection asks, and shared by every change on the route.
    """

    def __init__(self, route: Route, search: _Search, outputs: set[str]) -> None:
        self._route = route
        self._search = search
        self._outputs = outputs
        self._scanned = 0
        self._items: list[Candidate] = []

    def get(self, index: int) -> Candidate | None:
        while len(self._items) <= index:
            node_id = self._search.nth(self._scanned)
            if node_id is None:
                return None
            self._scanned += 1
            if node_id in self._outputs:
                self._items.append(_candidate(self._route, self._search, node_id))
        return self._items[index]


class _Walks:
    """Searches per start node and candidates per route, shared by all changes."""

    def __init__(self, graphs: dict[Side, SemanticGraph], outputs: dict[Side, set[str]]) -> None:
        self._outputs = outputs
        self._following = {side: _following(graph) for side, graph in graphs.items()}
        self._searches: dict[tuple[Side, str], _Search] = {}
        self._candidates: dict[tuple[Side, str, str | None], _Candidates] = {}

    def search(self, route: Route) -> _Search:
        """A route over an edge goes on from the edge's target."""
        start = route.via.target_node_id if route.via is not None else route.start
        key = (route.side, start)
        if key not in self._searches:
            self._searches[key] = _Search(self._following[route.side], start)
        return self._searches[key]

    def edges(self, route: Route, end: str) -> list[GraphEdge]:
        path = self.search(route).edges(end)
        return [route.via, *path] if route.via is not None else path

    def candidates(self, route: Route) -> _Candidates:
        key = (route.side, route.start, route.via.edge_id if route.via is not None else None)
        if key not in self._candidates:
            self._candidates[key] = _Candidates(
                route, self.search(route), self._outputs[route.side]
            )
        return self._candidates[key]


def _queue(routes: list[Route], walks: _Walks) -> Iterator[Candidate]:
    """One change's paths, fewest edges first; a tie goes to the earlier route, then BFS order."""
    lists = [walks.candidates(route) for route in routes]
    positions = [0] * len(lists)
    seen: set[Key] = set()
    while True:
        best: tuple[Candidate, int] | None = None
        for index, candidates in enumerate(lists):
            item = candidates.get(positions[index])
            if item is not None and (best is None or item[0] < best[0][0]):
                best = (item, index)
        if best is None:
            return
        item, index = best
        positions[index] += 1
        if item[1] not in seen:
            seen.add(item[1])
            yield item


def _select(
    placed: dict[str, list[Route]], walks: _Walks, limit: int | None
) -> tuple[set[Key], bool]:
    """Round robin over changes in ID order, each taking its shortest path not yet taken.

    Stops at the first new path past the limit, so candidates beyond it are never built.
    The flag says whether every path was seen, i.e. the kept paths are all of them.
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
            if item[1] in kept:
                continue
            if limit is not None and len(kept) == limit:
                return kept, False
            kept.add(item[1])
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

    With a limit, paths are built one at a time, every change's shortest first, and building
    stops at the first path past the limit.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    walks = _Walks(graphs, {side: output_nodes(graphs[side], facts[side]) for side in graphs})
    placed: dict[str, list[Route]] = {}
    unplaced = display_only = 0
    for change in changes:
        if change.kind == "DISPLAY_CHANGED":
            display_only += 1
            continue
        own = _own_route(change, graphs)
        if own is None:
            unplaced += 1
            continue
        placed[change.change_id] = [own, *_reference_routes(change, graphs)]
    kept, complete = _select(placed, walks, limit)
    wanted: dict[Side, list[Key]] = {}
    for key in sorted(kept):
        wanted.setdefault(key[0], []).append(key)
    # Every change and note behind a kept path, also from changes the selection never reached.
    found: dict[Key, tuple[str, list[GraphEdge], set[str], set[str]]] = {}
    uncovered = 0
    for change_id, routes in placed.items():
        covered = False
        for route in routes:
            search = walks.search(route)
            for key in wanted.get(route.side, []):
                _, end, edge_ids = key
                if not search.reaches(end):
                    continue
                edges = walks.edges(route, end)
                if tuple(edge.edge_id for edge in edges) != edge_ids:
                    continue
                _, _, change_ids, notes = found.setdefault(key, (route.start, edges, set(), set()))
                change_ids.add(change_id)
                if route.note is not None:
                    notes.add(route.note)
                covered = True
        # Only a stopped selection can leave a change that has a path without one.
        if not complete and not covered:
            if any(walks.candidates(route).get(0) is not None for route in routes):
                uncovered += 1
    paths = [
        _path(side, start, edges, sorted(change_ids), sorted(notes), facts[side], task_id)
        for (side, _, _), (start, edges, change_ids, notes) in sorted(found.items())
    ]
    return ImpactResult(paths, unplaced, display_only, complete, uncovered)
