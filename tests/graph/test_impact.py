import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flowdelta.contracts import Handoff, NormalizedWorkflow, SourceLocation
from flowdelta.graph import analyze
from flowdelta.graph.control import DATA_ORIGIN
from flowdelta.graph.impact import output_nodes, shortest_paths
from flowdelta.ingestion import json_bytes
from flowdelta.pipeline_contracts import AnalysisBundle, GraphEdge, ImpactPath, SemanticGraph
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline

ROOT = Path(__file__).resolve().parents[2]


def example(case: str) -> Path:
    return ROOT / "examples/contracts" / case


def load(case: str) -> Handoff:
    return read_handoff(example(case) / "input")


def checked(case: str, handoff: Handoff) -> AnalysisBundle:
    """Analyze, then run the cross-file check that guards impact path references."""
    result = analyze(handoff)
    examples = read_pipeline_examples(example(case))
    plan = examples.plan.model_copy(update={"analysis_id": result.analysis_id})
    validate_pipeline(handoff, result, examples.catalog, plan)
    return result


def with_priority_threshold(handoff: Handoff, threshold: int, operation: str = "gte") -> Handoff:
    nodes = []
    for node in handoff.after.nodes:
        if node.node_id == "priority":
            parameters = dict(node.parameters)
            conditions = dict(parameters["conditions"])
            condition = dict(conditions["conditions"][0])
            condition["rightValue"] = threshold
            condition["operator"] = {**condition["operator"], "operation": operation}
            conditions["conditions"] = [condition]
            parameters["conditions"] = conditions
            node = node.model_copy(update={"parameters": parameters})
        nodes.append(node)
    return handoff.model_copy(update={"after": handoff.after.model_copy(update={"nodes": nodes})})


def with_after_semantics(handoff: Handoff, node_id: str, **update: object) -> Handoff:
    items = [
        item.model_copy(update=update) if (item.node_id, item.side) == (node_id, "after") else item
        for item in handoff.semantics.semantics
    ]
    semantics = handoff.semantics.model_copy(update={"semantics": items})
    return handoff.model_copy(update={"semantics": semantics})


def without_after_semantics(handoff: Handoff, node_id: str) -> Handoff:
    items = [
        item
        for item in handoff.semantics.semantics
        if (item.node_id, item.side) != (node_id, "after")
    ]
    semantics = handoff.semantics.model_copy(update={"semantics": items})
    return handoff.model_copy(update={"semantics": semantics})


def without_after_node(handoff: Handoff, node_id: str) -> Handoff:
    after = handoff.after
    workflow: NormalizedWorkflow = after.model_copy(
        update={
            "nodes": [node for node in after.nodes if node.node_id != node_id],
            "connections": [
                item
                for item in after.connections
                if node_id not in (item.source_node_id, item.target_node_id)
            ],
        }
    )
    return handoff.model_copy(update={"after": workflow})


def without_after_connection(handoff: Handoff, source: str, target: str) -> Handoff:
    connections = [
        item
        for item in handoff.after.connections
        if (item.source_node_id, item.target_node_id) != (source, target)
    ]
    after = handoff.after.model_copy(update={"connections": connections})
    return handoff.model_copy(update={"after": after})


def with_after_connection(
    handoff: Handoff, source: str, target: str, kind: str = "main"
) -> Handoff:
    template = next(item for item in handoff.after.connections if item.source_node_id == source)
    added = template.model_copy(
        update={
            "target_node_id": target,
            "target_input_index": 0,
            "source_output_type": kind,
            "target_input_type": kind,
            "source": template.source.model_copy(
                update={"json_pointer": template.source.json_pointer[:-1] + "1"}
            ),
        }
    )
    after = handoff.after.model_copy(update={"connections": [*handoff.after.connections, added]})
    return handoff.model_copy(update={"after": after})


def hops(result: AnalysisBundle, path: ImpactPath) -> list[tuple[str, str]]:
    graph = result.before_graph if path.side == "before" else result.after_graph
    edges = {edge.edge_id: edge for edge in graph.edges}
    return [(edges[item].source_node_id, edges[item].target_node_id) for item in path.edge_ids]


def end_of(result: AnalysisBundle, path: ImpactPath, start: str) -> str:
    steps = hops(result, path)
    return steps[-1][1] if steps else start


def paths_of(result: AnalysisBundle, node_id: str) -> list[ImpactPath]:
    ids = {change.change_id for change in result.changes if change.node_id == node_id}
    return [path for path in result.impact_paths if ids & set(path.change_ids)]


def test_order_lookup_response_change_is_its_own_output() -> None:
    result = checked("order-lookup", load("order-lookup"))
    [change] = result.changes
    [path] = result.impact_paths
    assert path.side == "after"
    assert path.change_ids == [change.change_id]
    assert path.edge_ids == []
    assert path.task_ids == ["order-lookup"]
    assert path.policy_rule_ids == []
    assert any("정책" in item for item in path.limitations)
    assert path.support == "SUPPORTED"
    assert path.basis == "STATIC_POSSIBILITY"
    assert any("대표 최단 경로" in item and "실제 실행 경로" in item for item in result.limitations)


def test_support_triage_result_change_is_the_last_node() -> None:
    result = checked("support-triage", load("support-triage"))
    [path] = result.impact_paths
    assert path.edge_ids == []
    assert path.task_ids == ["support-triage"]
    assert [change.node_id for change in result.changes] == ["result"]


def test_receive_only_trigger_is_not_an_output() -> None:
    handoff = load("order-lookup")
    facts = {item.node_id: item for item in handoff.semantics.semantics if item.side == "after"}
    assert output_nodes(analyze(handoff).after_graph, facts) == {"read-order", "response"}


def test_if_change_reaches_subworkflow_call_and_last_node() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = checked("support-triage", handoff)
    paths = {end_of(result, path, "priority"): path for path in paths_of(result, "priority")}
    assert set(paths) == {"summary", "result"}
    # Equal-length branches are settled by edge ID: control-priority-standard < -urgent.
    assert hops(result, paths["summary"]) == [
        ("priority", "standard"),
        ("standard", "merge"),
        ("merge", "summary"),
    ]
    assert len(paths["result"].edge_ids) == 4
    for path in paths.values():
        assert path.side == "after"
        assert path.support == "SUPPORTED"
        assert any("하위 workflow" in item for item in path.limitations)


def test_two_changes_on_one_node_share_each_path() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4, operation="gt")
    result = checked("support-triage", handoff)
    ids = sorted(change.change_id for change in result.changes if change.node_id == "priority")
    assert len(ids) == 2
    paths = paths_of(result, "priority")
    assert len(paths) == 2
    assert all(path.change_ids == ids for path in paths)


def test_removed_node_path_uses_the_before_graph() -> None:
    result = checked("support-triage", without_after_node(load("support-triage"), "standard"))
    removed = next(change for change in result.changes if change.kind == "REMOVED")
    paths = [path for path in result.impact_paths if removed.change_id in path.change_ids]
    assert {end_of(result, path, "standard") for path in paths} == {"summary", "result"}
    for path in paths:
        assert path.side == "before"
        assert hops(result, path)[0][0] == "standard"
    # The removed connection priority -> standard exists only before, so it starts there too.
    dropped = next(
        change
        for change in result.changes
        if change.kind == "CONNECTION_CHANGED" and change.node_id == "priority"
    )
    dropped_paths = [path for path in result.impact_paths if dropped.change_id in path.change_ids]
    assert dropped_paths
    for path in dropped_paths:
        assert path.side == "before"
        assert hops(result, path)[0] == ("priority", "standard")


def test_removed_connection_path_starts_with_that_connection() -> None:
    # Without the rule the tie-break would pick priority -> standard and hide urgent.
    result = checked(
        "support-triage", without_after_connection(load("support-triage"), "priority", "urgent")
    )
    [dropped] = [change for change in result.changes if change.kind == "CONNECTION_CHANGED"]
    paths = [path for path in result.impact_paths if dropped.change_id in path.change_ids]
    assert {end_of(result, path, "priority") for path in paths} == {"summary", "result"}
    for path in paths:
        assert path.side == "before"
        assert hops(result, path)[0] == ("priority", "urgent")


def test_added_connection_path_starts_with_that_connection() -> None:
    result = checked(
        "support-triage", with_after_connection(load("support-triage"), "seed", "result")
    )
    [added] = [change for change in result.changes if change.kind == "CONNECTION_CHANGED"]
    paths = [path for path in result.impact_paths if added.change_id in path.change_ids]
    assert [(path.side, hops(result, path)) for path in paths] == [("after", [("seed", "result")])]


def test_removed_parameter_on_a_kept_node_starts_after() -> None:
    handoff = load("support-triage")
    nodes = []
    for node in handoff.after.nodes:
        if node.node_id == "priority":
            parameters = {k: v for k, v in node.parameters.items() if k != "looseTypeValidation"}
            node = node.model_copy(update={"parameters": parameters})
        nodes.append(node)
    after = handoff.after.model_copy(update={"nodes": nodes})
    result = checked("support-triage", handoff.model_copy(update={"after": after}))
    [removed] = [change for change in result.changes if change.node_id == "priority"]
    assert removed.after_source is None
    paths = [path for path in result.impact_paths if removed.change_id in path.change_ids]
    assert paths
    assert {path.side for path in paths} == {"after"}


def test_node_with_unknown_effects_is_an_output() -> None:
    handoff = with_after_semantics(
        with_priority_threshold(load("support-triage"), 4),
        "urgent",
        support="UNSUPPORTED",
        reads=None,
        writes=None,
        effects=None,
    )
    result = checked("support-triage", handoff)
    paths = {end_of(result, path, "priority"): path for path in paths_of(result, "priority")}
    assert hops(result, paths["urgent"]) == [("priority", "urgent")]
    assert paths["urgent"].support == "PARTIAL"
    assert any("urgent" in item for item in paths["urgent"].limitations)


def test_unsupported_start_record_is_unknown() -> None:
    handoff = with_after_semantics(
        load("order-lookup"),
        "response",
        support="UNSUPPORTED",
        reads=None,
        writes=None,
        effects=None,
    )
    [path] = analyze(handoff).impact_paths
    assert path.support == "UNKNOWN"


def test_multi_hop_paths_are_stable_across_runs() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4)
    first, second = analyze(handoff), analyze(handoff)
    assert len(first.impact_paths) == 3
    assert json_bytes(first.model_dump(mode="json")) == json_bytes(second.model_dump(mode="json"))


def test_unsupported_node_on_the_path_is_partial() -> None:
    handoff = with_after_semantics(
        with_priority_threshold(load("support-triage"), 4), "merge", support="UNSUPPORTED"
    )
    result = checked("support-triage", handoff)
    paths = paths_of(result, "priority")
    assert paths
    for path in paths:
        assert path.support == "PARTIAL"
        assert any("merge" in item for item in path.limitations)


def test_start_without_semantics_is_unknown() -> None:
    result = analyze(without_after_semantics(load("order-lookup"), "response"))
    [path] = result.impact_paths
    assert path.support == "UNKNOWN"


def test_workflow_level_change_has_no_path() -> None:
    handoff = load("order-lookup")
    settings = {"executionOrder": "v0"}
    after = handoff.after.model_copy(
        update={"opaque_fields": {**handoff.after.opaque_fields, "settings": settings}}
    )
    result = checked("order-lookup", handoff.model_copy(update={"after": after}))
    workflow_level = [change for change in result.changes if change.node_id is None]
    assert len(workflow_level) == 1
    assert all(workflow_level[0].change_id not in path.change_ids for path in result.impact_paths)
    assert len(result.impact_paths) == 1
    assert any("시작 노드가 없는 변경 1개" in item for item in result.limitations)
    assert not any("아직 계산하지 않았다" in item for item in result.limitations)


def edge(source: str, target: str) -> GraphEdge:
    return GraphEdge(
        edge_id=f"control-{source}-{target}",
        source_node_id=source,
        target_node_id=target,
        relation="CONTROL",
        output_port=0,
        input_port=0,
        evidence=[SourceLocation(artifact_id="wf-1", json_pointer="/connections")],
        basis="STATIC_POSSIBILITY",
    )


def test_cycle_terminates() -> None:
    graph = SemanticGraph(
        node_ids=["a", "b", "c"],
        edges=[edge("a", "b"), edge("b", "a"), edge("b", "c")],
        limitations=[],
    )
    paths = shortest_paths(graph, "a")
    assert set(paths) == {"a", "b", "c"}
    assert [item.edge_id for item in paths["c"]] == ["control-a-b", "control-b-c"]
    assert paths["a"] == []


def renamed(handoff: Handoff, names: dict[str, str], *, rewrite: bool = False) -> Handoff:
    """Rename nodes in after; with rewrite, $('old') references follow the new names."""
    nodes = []
    for node in handoff.after.nodes:
        if node.node_id in names:
            node = node.model_copy(update={"name": names[node.node_id]})
        if rewrite:
            text = json_bytes(node.parameters).decode("utf-8")
            for node_id, name in names.items():
                old = next(item.name for item in handoff.before.nodes if item.node_id == node_id)
                text = text.replace(f"$('{old}')", f"$('{name}')")
            node = node.model_copy(update={"parameters": json.loads(text)})
        nodes.append(node)
    return handoff.model_copy(update={"after": handoff.after.model_copy(update={"nodes": nodes})})


def rename_paths(result: AnalysisBundle, node_id: str) -> list[ImpactPath]:
    change = next(
        item
        for item in result.changes
        if item.node_id == node_id
        and item.before_source is not None
        and item.before_source.json_pointer.endswith("/name")
    )
    return [path for path in result.impact_paths if change.change_id in path.change_ids]


def first_hops(result: AnalysisBundle, paths: list[ImpactPath], side: str) -> set[tuple[str, str]]:
    return {hops(result, path)[0] for path in paths if path.side == side and path.edge_ids}


def test_broken_reference_is_followed_in_the_before_graph() -> None:
    result = checked("support-triage", renamed(load("support-triage"), {"split": "Split items"}))
    paths = rename_paths(result, "split")
    # The name references are gone after the rename, so the lost relation starts before.
    assert first_hops(result, paths, "before") == {("split", "urgent"), ("split", "standard")}
    lost = [path for path in paths if path.side == "before"]
    assert {end_of(result, path, "split") for path in lost} == {"summary", "result"}
    assert all(any("이름 참조" in item for item in path.limitations) for path in lost)
    # The rename itself still starts at split in the after graph as before.
    assert any(path.side == "after" for path in paths)


def test_retargeted_reference_also_starts_at_the_new_holder() -> None:
    handoff = renamed(load("support-triage"), {"split": "Split items", "seed": "Split tickets"})
    result = checked("support-triage", handoff)
    paths = rename_paths(result, "split")
    assert first_hops(result, paths, "before") == {("split", "urgent"), ("split", "standard")}
    assert {("seed", "urgent"), ("seed", "standard")} <= first_hops(result, paths, "after")


def test_rename_with_updated_references_stays_after() -> None:
    handoff = renamed(load("support-triage"), {"split": "Split items"}, rewrite=True)
    result = checked("support-triage", handoff)
    paths = rename_paths(result, "split")
    assert paths
    assert {path.side for path in paths} == {"after"}


def test_display_only_change_has_no_path() -> None:
    handoff = load("order-lookup")
    nodes = [
        node.model_copy(update={"opaque_fields": {**node.opaque_fields, "position": [999, 999]}})
        if node.node_id == "read-order"
        else node
        for node in handoff.after.nodes
    ]
    after = handoff.after.model_copy(update={"nodes": nodes})
    result = checked("order-lookup", handoff.model_copy(update={"after": after}))
    [display] = [change for change in result.changes if change.kind == "DISPLAY_CHANGED"]
    assert all(display.change_id not in path.change_ids for path in result.impact_paths)
    assert len(result.impact_paths) == 1
    assert any("표시 변경 1개" in item for item in result.limitations)


def edit_assignments(
    workflow: NormalizedWorkflow, node_id: str, edit: Callable[[list[Any]], list[Any]]
) -> NormalizedWorkflow:
    nodes = []
    for node in workflow.nodes:
        if node.node_id == node_id:
            parameters = dict(node.parameters)
            block = parameters["assignments"]
            assert isinstance(block, dict) and isinstance(block["assignments"], list)
            items = edit(list(block["assignments"]))
            parameters["assignments"] = {**block, "assignments": items}
            node = node.model_copy(update={"parameters": parameters})
        nodes.append(node)
    return workflow.model_copy(update={"nodes": nodes})


def second_reference(items: list[Any]) -> list[Any]:
    extra = {
        "id": "urgent-2",
        "name": "source_queue",
        "type": "string",
        "value": "={{ $('Split tickets').item.json.queue }}",
    }
    return [*items, extra]


def name_reference_edge(graph: SemanticGraph, target: str, pointer_end: str) -> str:
    return next(
        edge.edge_id
        for edge in graph.edges
        if edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE"
        and edge.target_node_id == target
        and edge.evidence[0].json_pointer.endswith(pointer_end)
    )


def test_only_the_reference_left_on_the_old_name_is_followed_before() -> None:
    handoff = load("support-triage")
    before = edit_assignments(handoff.before, "urgent", second_reference)
    after = edit_assignments(handoff.after, "urgent", second_reference)

    def update_first(items: list[Any]) -> list[Any]:
        first = {**items[0], "value": items[0]["value"].replace("Split tickets", "Split items")}
        return [first, *items[1:]]

    after = edit_assignments(after, "urgent", update_first)
    handoff = handoff.model_copy(update={"before": before, "after": after})
    result = analyze(renamed(handoff, {"split": "Split items"}))
    lost = {
        path.edge_ids[0]
        for path in rename_paths(result, "split")
        if path.side == "before" and path.edge_ids
    }
    # urgent's first reference was updated, its second was not; standard was not updated.
    assert lost == {
        name_reference_edge(result.before_graph, "urgent", "/assignments/assignments/2/value"),
        name_reference_edge(result.before_graph, "standard", "/assignments/assignments/0/value"),
    }


def test_moved_reference_on_the_old_name_is_still_followed() -> None:
    handoff = load("support-triage")
    # Reordering moves urgent's reference from /0/value to /1/value without updating it.
    after = edit_assignments(handoff.after, "urgent", lambda items: list(reversed(items)))
    handoff = handoff.model_copy(update={"after": after})
    result = analyze(renamed(handoff, {"split": "Split items"}))
    paths = [path for path in rename_paths(result, "split") if path.side == "before"]
    assert first_hops(result, paths, "before") == {("split", "urgent"), ("split", "standard")}
    moved = [path for path in paths if hops(result, path)[0] == ("split", "urgent")]
    assert all(any("위치" in item for item in path.limitations) for path in moved)


def test_path_over_an_ai_connection_says_it_is_not_execution_order() -> None:
    handoff = with_after_connection(load("support-triage"), "seed", "result", kind="ai_tool")
    result = checked("support-triage", handoff)
    [added] = [change for change in result.changes if change.kind == "CONNECTION_CHANGED"]
    [path] = [path for path in result.impact_paths if added.change_id in path.change_ids]
    assert hops(result, path) == [("seed", "result")]
    assert any("ai_tool" in item and "실행 순서" in item for item in path.limitations)
