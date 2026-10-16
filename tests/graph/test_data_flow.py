from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from flowdelta.contracts import (
    Connection,
    ErrorBehavior,
    Handoff,
    NodeSemantics,
    NormalizedNode,
    NormalizedWorkflow,
    SourceLocation,
)
from flowdelta.graph import analyze
from flowdelta.graph.control import DATA_ORIGIN, build_control_graph
from flowdelta.graph.data_flow import PASS_THROUGH, add_field_flow_edges
from flowdelta.pipeline_contracts import SemanticGraph
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2]
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


def connect(source: str, target: str, port: int = 0, output: int = 0) -> Connection:
    return Connection(
        source_node_id=source,
        source_output_type="main",
        source_output_index=output,
        target_node_id=target,
        target_input_type="main",
        target_input_index=port,
        opaque_fields={},
        source=SourceLocation(
            artifact_id=ARTIFACT, json_pointer=f"/connections/{source}/main/{output}/{port}"
        ),
    )


def facts(
    node_id: str,
    *,
    reads: Sequence[str] | None = (),
    writes: Sequence[str] | None = (),
    support: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"] = "SUPPORTED",
    error_output: int | None = None,
) -> NodeSemantics:
    error = (
        ErrorBehavior(on_error="continueErrorOutput", error_output_index=error_output)
        if error_output is not None
        else None
    )
    return NodeSemantics(
        node_id=node_id,
        side="after",
        node_type="n8n-nodes-base.set",
        type_version=3.4,
        operation=None,
        reads=list(reads) if reads is not None else None,
        writes=list(writes) if writes is not None else None,
        support=support,
        error_behavior=error,
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


def load(case: str) -> Handoff:
    return read_handoff(ROOT / "examples/contracts" / case / "input")


def test_order_lookup_field_flow_on_both_sides() -> None:
    result = analyze(load("order-lookup"))
    expected = {
        ("request", "read-order"): (["$json.body"], ["$json.body.order_id"]),
        ("read-order", "response"): (
            ["$json.order_id", "$json.status"],
            ["$json.order_id", "$json.status"],
        ),
    }
    assert flows(result.before_graph) == expected
    assert flows(result.after_graph) == expected


def test_support_triage_field_flow_stops_at_subworkflow_call() -> None:
    graph = analyze(load("support-triage")).after_graph
    assert flows(graph) == {
        ("seed", "split"): (["$json.tickets"], ["$json.tickets"]),
        ("split", "priority"): (["$json.*:array-element"], ["$json.priority"]),
    }
    assert any(
        "result가 읽는 $json.queue" in item and "summary" in item for item in graph.limitations
    )


def test_fixed_subworkflow_gets_its_own_graph() -> None:
    result = analyze(load("support-triage"))
    assert list(result.dependency_graphs) == ["summary"]
    graph = result.dependency_graphs["summary"]
    controls = {
        (edge.source_node_id, edge.target_node_id)
        for edge in graph.edges
        if edge.relation == "CONTROL"
    }
    assert controls == {("child-entry", "child-public")}
    assert flows(graph) == {
        ("child-entry", "child-public"): (
            ["$json.*:caller-input"],
            ["$json.queue", "$json.ticket_id"],
        )
    }
    assert not any("전후 의미 정보가 달라" in item for item in graph.limitations)
    assert not any("아직 만들지 않았다" in item for item in result.limitations)
    assert any("dependency_graphs" in item for item in result.limitations)


def test_subworkflow_semantics_that_differ_by_side_are_noted() -> None:
    handoff = load("support-triage")
    index = handoff.dependency_semantics["summary"]
    changed = [
        item.model_copy(update={"reads": []})
        if (item.node_id, item.side) == ("child-public", "before")
        else item
        for item in index.semantics
    ]
    dependency_semantics = {"summary": index.model_copy(update={"semantics": changed})}
    result = analyze(handoff.model_copy(update={"dependency_semantics": dependency_semantics}))
    graph = result.dependency_graphs["summary"]
    [note] = [item for item in graph.limitations if "전후 의미 정보가 달라" in item]
    assert "child-public" in note and "child-entry" not in note
    assert "after 쪽" in note
    assert set(flows(graph)) == {("child-entry", "child-public")}


def test_subworkflow_with_only_before_semantics_says_so() -> None:
    handoff = load("support-triage")
    index = handoff.dependency_semantics["summary"]
    before_only = [item for item in index.semantics if item.side == "before"]
    dependency_semantics = {"summary": index.model_copy(update={"semantics": before_only})}
    result = analyze(handoff.model_copy(update={"dependency_semantics": dependency_semantics}))
    graph = result.dependency_graphs["summary"]
    assert any("before 쪽" in item for item in graph.limitations)
    assert set(flows(graph)) == {("child-entry", "child-public")}


def test_preserved_input_passes_fields_it_does_not_write() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [
            facts("a", writes=["$json.x"]),
            facts("b", writes=["$json.y", "$json.*:preserved-input"]),
            facts("c", reads=["$json.x", "$json.y"]),
        ],
    )
    assert flows(graph) == {
        ("a", "c"): (["$json.x"], ["$json.x"]),
        ("b", "c"): (["$json.y"], ["$json.y"]),
    }
    assert added_limitations(graph) == []


def test_every_matching_write_is_recorded() -> None:
    graph = graph_of(
        ["a", "b"],
        [connect("a", "b")],
        [facts("a", writes=["$json.body", "$json.body.id"]), facts("b", reads=["$json.body.id"])],
    )
    assert flows(graph) == {("a", "b"): (["$json.body", "$json.body.id"], ["$json.body.id"])}


def test_nearer_writer_hides_a_farther_one() -> None:
    graph = graph_of(
        ["a", "b", "c"],
        [connect("a", "b"), connect("b", "c")],
        [
            facts("a", writes=["$json.x"]),
            facts("b", writes=["$json.x"]),
            facts("c", reads=["$json.x"]),
        ],
    )
    assert flows(graph) == {("b", "c"): (["$json.x"], ["$json.x"])}


def test_error_output_does_not_carry_declared_writes() -> None:
    graph = graph_of(
        ["a", "b"],
        [connect("a", "b", output=1)],
        [facts("a", writes=["$json.status"], error_output=1), facts("b", reads=["$json.status"])],
    )
    assert flows(graph) == {}
    assert any("오류 출력" in item and "a" in item for item in added_limitations(graph))


def test_pass_through_root_is_named() -> None:
    graph = graph_of(
        ["a", "c"],
        [connect("a", "c")],
        [facts("a", writes=[PASS_THROUGH]), facts("c", reads=["$json.x"])],
    )
    assert flows(graph) == {}
    assert any("a보다 앞 노드가 없어" in item for item in added_limitations(graph))


def test_loop_back_to_the_reader_is_noted() -> None:
    graph = graph_of(
        ["a", "c", "b"],
        [connect("a", "c"), connect("c", "b"), connect("b", "c", 1)],
        [
            facts("a", writes=["$json.x"]),
            facts("c", reads=["$json.x"], writes=["$json.y"]),
            facts("b", writes=[PASS_THROUGH]),
        ],
    )
    assert flows(graph) == {("a", "c"): (["$json.x"], ["$json.x"])}
    assert any("c 자신에게 돌아와" in item for item in added_limitations(graph))


def test_subworkflow_branch_difference_is_noted() -> None:
    handoff = load("support-triage")
    branches = next(
        item.branches
        for item in handoff.semantics.semantics
        if (item.node_id, item.side) == ("priority", "before")
    )
    assert branches
    index = handoff.dependency_semantics["summary"]
    changed = [
        item.model_copy(update={"branches": branches})
        if (item.node_id, item.side) == ("child-public", "before")
        else item
        for item in index.semantics
    ]
    dependency_semantics = {"summary": index.model_copy(update={"semantics": changed})}
    result = analyze(handoff.model_copy(update={"dependency_semantics": dependency_semantics}))
    graph = result.dependency_graphs["summary"]
    assert any(
        "전후 의미 정보가 달라" in item and "child-public" in item for item in graph.limitations
    )


def test_graph_explains_how_to_tell_data_edges_apart() -> None:
    graph = analyze(load("order-lookup")).after_graph
    assert any(
        "flowdelta.graph.data_origin" in item and "FIELD_FLOW" in item and "JSON Pointer" in item
        for item in graph.limitations
    )
