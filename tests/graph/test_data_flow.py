from collections.abc import Sequence
from typing import Literal

from flowdelta.contracts import (
    Connection,
    NodeSemantics,
    NormalizedNode,
    NormalizedWorkflow,
    SourceLocation,
)
from flowdelta.graph.control import DATA_ORIGIN, build_control_graph
from flowdelta.graph.data_flow import PASS_THROUGH, add_field_flow_edges
from flowdelta.pipeline_contracts import SemanticGraph

ARTIFACT = "wf-1"
Flows = dict[tuple[str, str], tuple[list[str], list[str]]]


def node(index: int, node_id: str) -> NormalizedNode:
    return NormalizedNode(
        node_id=node_id,
        name=node_id.title(),
        node_type="n8n-nodes-base.set",
        type_version=3.4,
        parameters={},
        opaque_fields={},
        source=SourceLocation(artifact_id=ARTIFACT, json_pointer=f"/nodes/{index}"),
    )


def connect(source: str, target: str, port: int = 0) -> Connection:
    return Connection(
        source_node_id=source,
        source_output_type="main",
        source_output_index=0,
        target_node_id=target,
        target_input_type="main",
        target_input_index=port,
        opaque_fields={},
        source=SourceLocation(
            artifact_id=ARTIFACT, json_pointer=f"/connections/{source}/main/0/{port}"
        ),
    )


def facts(
    node_id: str,
    *,
    reads: Sequence[str] | None = (),
    writes: Sequence[str] | None = (),
    support: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"] = "SUPPORTED",
) -> NodeSemantics:
    return NodeSemantics(
        node_id=node_id,
        side="after",
        node_type="n8n-nodes-base.set",
        type_version=3.4,
        operation=None,
        reads=list(reads) if reads is not None else None,
        writes=list(writes) if writes is not None else None,
        support=support,
        source=SourceLocation(artifact_id=ARTIFACT, json_pointer=f"/nodes/{node_id}"),
    )


def graph_of(
    node_ids: list[str], connections: list[Connection], semantics: list[NodeSemantics]
) -> SemanticGraph:
    workflow = NormalizedWorkflow(
        source_artifact_id=ARTIFACT,
        workflow_id=None,
        nodes=[node(index, node_id) for index, node_id in enumerate(node_ids)],
        connections=connections,
        opaque_fields={},
        original_connections={},
        limitations=[],
    )
    by_id = {item.node_id: item for item in semantics}
    return add_field_flow_edges(build_control_graph(workflow, by_id), workflow, by_id)


def flows(graph: SemanticGraph) -> Flows:
    return {
        (edge.source_node_id, edge.target_node_id): (edge.source_fields, edge.target_fields)
        for edge in graph.edges
        if edge.extensions.get(DATA_ORIGIN) == "FIELD_FLOW"
    }


def added_limitations(graph: SemanticGraph) -> list[str]:
    """Field-flow limitations carry the reader's location; the generic graph notes do not."""
    return [item for item in graph.limitations if f"{ARTIFACT}#" in item]


def test_pass_through_nodes_are_crossed() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [
            facts("a", writes=["$json.x"]),
            facts("b", writes=[PASS_THROUGH]),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert flows(graph) == {("a", "c"): (["$json.x"], ["$json.x"])}
    edge = next(item for item in graph.edges if item.relation == "DATA")
    assert edge.basis == "STATIC_POSSIBILITY"
    assert [item.json_pointer for item in edge.evidence] == ["/nodes/0", "/nodes/2"]


def test_every_branch_is_followed() -> None:
    graph = graph_of(
        ["a1", "a2", "m", "c"],
        [connect("a1", "m", 0), connect("a2", "m", 1), connect("m", "c")],
        [
            facts("a1", writes=["$json.x"]),
            facts("a2", writes=["$json.x"]),
            facts("m", writes=[PASS_THROUGH]),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert set(flows(graph)) == {("a1", "c"), ("a2", "c")}


def test_partial_branch_keeps_edge_and_limitation() -> None:
    graph = graph_of(
        ["a1", "a2", "m", "c"],
        [connect("a1", "m", 0), connect("a2", "m", 1), connect("m", "c")],
        [
            facts("a1", writes=["$json.x"]),
            facts("a2", writes=["$json.y"]),
            facts("m", writes=[PASS_THROUGH]),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert set(flows(graph)) == {("a1", "c")}
    assert any("a2" in item and "$json.x" in item for item in added_limitations(graph))


def test_parent_and_child_fields_match() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [
            facts("a", writes=["$json.body"]),
            facts("b", reads=["$json.body.order_id"], writes=["$json.result.id"]),
            facts("c", reads=["$json.result"]),
        ],
    )
    assert flows(graph) == {
        ("a", "b"): (["$json.body"], ["$json.body.order_id"]),
        ("b", "c"): (["$json.result.id"], ["$json.result"]),
    }


def test_fields_of_one_pair_share_an_edge() -> None:
    graph = graph_of(
        ["a", "c"],
        [connect("a", "c")],
        [facts("a", writes=["$json.x", "$json.y"]), facts("c", reads=["$json.y", "$json.x"])],
    )
    assert flows(graph) == {("a", "c"): (["$json.x", "$json.y"], ["$json.x", "$json.y"])}


def test_wildcard_writer_supplies_any_field() -> None:
    graph = graph_of(
        ["a", "c"],
        [connect("a", "c")],
        [facts("a", writes=["$json.*:array-element"]), facts("c", reads=["$json.priority"])],
    )
    assert flows(graph) == {("a", "c"): (["$json.*:array-element"], ["$json.priority"])}


def test_undeclared_node_stops_the_search() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [facts("a", writes=["$json.x"]), facts("b"), facts("c", reads=["$json.x"])],
    )
    assert flows(graph) == {}
    assert any(" b" in item and "$json.x" in item for item in added_limitations(graph))


def test_unsupported_node_stops_the_search() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [
            facts("a", writes=["$json.x"]),
            facts("b", writes=[PASS_THROUGH], support="UNKNOWN"),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert flows(graph) == {}
    assert any("지원되지 않는 b" in item for item in added_limitations(graph))


def test_reader_without_upstream_is_a_limitation() -> None:
    graph = graph_of(["c"], [], [facts("c", reads=["$json.x"])])
    assert flows(graph) == {}
    assert any("앞 노드가 없다" in item for item in added_limitations(graph))


def test_named_and_http_reads_are_left_to_other_rules() -> None:
    graph = graph_of(
        ["a", "c"],
        [connect("a", "c")],
        [facts("a"), facts("c", reads=["$('A').item.json.x", "http.request.body"])],
    )
    assert flows(graph) == {}
    assert added_limitations(graph) == []


def test_reader_without_semantics_is_a_limitation() -> None:
    graph = graph_of(["a", "c"], [connect("a", "c")], [facts("a", writes=["$json.x"])])
    assert flows(graph) == {}
    assert any("c" in item and "필드 흐름을 계산하지 않았다" in item for item in graph.limitations)


def test_unknown_read_form_is_a_limitation() -> None:
    graph = graph_of(
        ["a", "c"],
        [connect("a", "c")],
        [facts("a", writes=["$json.x"]), facts("c", reads=["$input.first().json.x"])],
    )
    assert flows(graph) == {}
    assert any("해석하지 않는 형식" in item for item in added_limitations(graph))


def test_cycle_terminates() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "a"), connect("b", "c")],
        [
            facts("a", writes=[PASS_THROUGH]),
            facts("b", writes=[PASS_THROUGH]),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert flows(graph) == {}
    assert any("찾지 못했다" in item for item in added_limitations(graph))
