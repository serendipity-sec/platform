from pathlib import Path
from typing import Any

import pytest

import flowdelta.graph.impact as impact
from flowdelta.contracts import Handoff, SourceLocation
from flowdelta.graph import AnalysisLimits, analyze
from flowdelta.graph.control import DATA_ORIGIN
from flowdelta.graph.references import EXTENSION as NAME_REFERENCES
from flowdelta.ingestion import json_bytes
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    Change,
    GraphEdge,
    ImpactPath,
    SemanticGraph,
)
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline

ROOT = Path(__file__).resolve().parents[2]
LIMITS = "flowdelta.graph.limits"


def example(case: str) -> Path:
    return ROOT / "examples/contracts" / case


def load(case: str) -> Handoff:
    return read_handoff(example(case) / "input")


def checked(case: str, handoff: Handoff, limits: AnalysisLimits) -> AnalysisBundle:
    result = analyze(handoff, limits)
    examples = read_pipeline_examples(example(case))
    plan = examples.plan.model_copy(update={"analysis_id": result.analysis_id})
    validate_pipeline(handoff, result, examples.catalog, plan)
    return result


def record(result: AnalysisBundle) -> dict[str, Any]:
    value = result.extensions[LIMITS]
    assert isinstance(value, dict)
    return value


def field_flows(graph: SemanticGraph) -> list[str]:
    return [
        edge.edge_id for edge in graph.edges if edge.extensions.get(DATA_ORIGIN) == "FIELD_FLOW"
    ]


def with_priority_threshold(handoff: Handoff, threshold: int) -> Handoff:
    nodes = []
    for node in handoff.after.nodes:
        if node.node_id == "priority":
            parameters = dict(node.parameters)
            conditions = parameters["conditions"]
            assert isinstance(conditions, dict)
            items = conditions["conditions"]
            assert isinstance(items, list) and isinstance(items[0], dict)
            changed = {**conditions, "conditions": [{**items[0], "rightValue": threshold}]}
            node = node.model_copy(update={"parameters": {**parameters, "conditions": changed}})
        nodes.append(node)
    return handoff.model_copy(update={"after": handoff.after.model_copy(update={"nodes": nodes})})


@pytest.mark.parametrize("case", ["order-lookup", "support-triage"])
def test_examples_are_within_the_default_limits(case: str) -> None:
    result = checked(case, load(case), AnalysisLimits())
    assert record(result) == {
        "max_workflow_nodes": 100,
        "max_snapshot_nodes": 300,
        "max_impact_paths": 20,
        "truncated": False,
        "reasons": [],
        "impact_paths": {"computed": True, "complete": True, "total": 1, "kept": 1},
    }
    assert not any("상한" in item for item in result.limitations)


def test_path_limit_keeps_each_changes_shortest_path_first() -> None:
    # Two changes: result (one 0-edge path) and priority (summary in 3 edges, result in 4).
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = checked("support-triage", handoff, AnalysisLimits(max_impact_paths=2))
    assert record(result)["truncated"] is True
    # Generation stopped at the limit, so the total is not known.
    assert record(result)["reasons"] == [{"code": "IMPACT_PATH_LIMIT", "count": None, "limit": 2}]
    assert record(result)["impact_paths"] == {
        "computed": True,
        "complete": False,
        "total": None,
        "kept": 2,
    }
    kept = sorted(len(path.edge_ids) for path in result.impact_paths)
    assert kept == [0, 3]
    assert {change_id for path in result.impact_paths for change_id in path.change_ids} == {
        change.change_id for change in result.changes
    }
    assert any("상한 2개를 넘어 2개를 만든 뒤" in item for item in result.limitations)
    again = analyze(handoff, AnalysisLimits(max_impact_paths=2))
    assert json_bytes(again.model_dump(mode="json")) == json_bytes(result.model_dump(mode="json"))


def test_path_limit_below_the_change_count_reports_uncovered_changes() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = analyze(handoff, AnalysisLimits(max_impact_paths=1))
    assert len(result.impact_paths) == 1
    assert any("경로가 하나도 남지 않은 변경은 1개" in item for item in result.limitations)


def test_path_limit_equal_to_the_path_count_keeps_an_exact_total() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = checked("support-triage", handoff, AnalysisLimits(max_impact_paths=3))
    assert record(result)["truncated"] is False
    assert record(result)["impact_paths"] == {
        "computed": True,
        "complete": True,
        "total": 3,
        "kept": 3,
    }
    assert not any("상한" in item for item in result.limitations)


def with_renamed_split(handoff: Handoff) -> Handoff:
    # References still call the old name, so routes also start from the lost references.
    nodes = [
        node.model_copy(update={"name": "Split items"}) if node.node_id == "split" else node
        for node in handoff.after.nodes
    ]
    return handoff.model_copy(update={"after": handoff.after.model_copy(update={"nodes": nodes})})


@pytest.mark.parametrize("limit", range(11))
def test_budgeted_generation_keeps_paths_of_the_full_result(limit: int) -> None:
    handoff = with_renamed_split(with_priority_threshold(load("support-triage"), 4))
    every = analyze(handoff, AnalysisLimits(max_impact_paths=1000)).impact_paths
    assert len(every) == 9
    result = analyze(handoff, AnalysisLimits(max_impact_paths=limit))
    assert len(result.impact_paths) == min(limit, 9)
    # The same paths, each still listing every change behind it.
    same = {path.path_id: path for path in every}
    assert all(path == same[path.path_id] for path in result.impact_paths)
    assert record(result)["impact_paths"]["complete"] is (limit >= 9)
    # Every change takes a turn before any second path, so each first keeps one of its shortest.
    changes = sorted({item for path in every for item in path.change_ids})
    if limit >= len(changes):
        for change_id in changes:
            shortest = min(len(path.edge_ids) for path in every if change_id in path.change_ids)
            assert any(
                change_id in path.change_ids and len(path.edge_ids) == shortest
                for path in result.impact_paths
            )


def node_change(node_id: str) -> Change:
    location = SourceLocation(artifact_id="wf-1", json_pointer="/nodes/0/parameters/value")
    return Change(
        change_id=f"change-{node_id}",
        node_id=node_id,
        kind="PARAMETER_CHANGED",
        before_value=1,
        after_value=2,
        before_source=location,
        after_source=location,
    )


def control_edge(source: str, target: str) -> GraphEdge:
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


def counted_candidates(
    monkeypatch: pytest.MonkeyPatch, graph: SemanticGraph, start: str, limit: int
) -> tuple[impact.ImpactResult, list[str]]:
    """Run one change at start; every node is an output (no semantics). Returns built ends."""
    built: list[str] = []
    original = impact._candidate

    def counting(route: impact.Route, search: Any, end: str) -> impact.Candidate:
        built.append(end)
        return original(route, search, end)

    monkeypatch.setattr(impact, "_candidate", counting)
    graphs: dict[impact.Side, SemanticGraph] = {"before": graph, "after": graph}
    result = impact.impact_paths(
        [node_change(start)], graphs, {"before": {}, "after": {}}, "task-1", limit
    )
    return result, built


def test_candidates_stop_at_the_first_path_past_the_limit_on_a_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [f"n{index:02}" for index in range(50)]
    edges = [control_edge(left, right) for left, right in zip(nodes, nodes[1:], strict=False)]
    graph = SemanticGraph(node_ids=nodes, edges=edges, limitations=[])
    result, built = counted_candidates(monkeypatch, graph, "n00", 2)
    assert [len(path.edge_ids) for path in result.paths] == [0, 1]
    assert result.complete is False
    # Two kept and the third that shows there are more; the other 47 are never built.
    assert built == ["n00", "n01", "n02"]


def test_candidates_stop_at_the_first_path_past_the_limit_on_a_wide_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = [f"leaf-{index:02}" for index in range(50)]
    edges = [control_edge("hub", leaf) for leaf in leaves]
    graph = SemanticGraph(node_ids=["hub", *leaves], edges=edges, limitations=[])
    result, built = counted_candidates(monkeypatch, graph, "hub", 3)
    assert len(result.paths) == 3
    assert result.complete is False
    # Fifty paths of one edge tie; candidates are still built one at a time, in edge-ID order.
    assert built == ["hub", "leaf-00", "leaf-01", "leaf-02"]


def test_candidates_cover_everything_when_the_limit_is_not_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaves = [f"leaf-{index:02}" for index in range(5)]
    edges = [control_edge("hub", leaf) for leaf in leaves]
    graph = SemanticGraph(node_ids=["hub", *leaves], edges=edges, limitations=[])
    result, built = counted_candidates(monkeypatch, graph, "hub", 20)
    assert len(result.paths) == 6
    assert result.complete is True
    assert sorted(built) == sorted(["hub", *leaves])


def test_many_reference_routes_build_only_the_paths_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed node whose 100 name references all broke: 100 routes reach the same output."""
    pointers = [f"/nodes/1/parameters/p{index:03}" for index in range(100)]
    references = [
        GraphEdge(
            edge_id=f"data-{index:03}",
            source_node_id="a",
            target_node_id="b",
            relation="DATA",
            output_port=0,
            input_port=0,
            target_fields=[pointer],
            evidence=[SourceLocation(artifact_id="wf-1", json_pointer=pointer)],
            basis="STATIC_POSSIBILITY",
            extensions={DATA_ORIGIN: "NAME_REFERENCE"},
        )
        for index, pointer in enumerate(pointers)
    ]
    before = SemanticGraph(node_ids=["a", "b"], edges=references, limitations=[])
    after = SemanticGraph(node_ids=["a", "b"], edges=[], limitations=[])
    name = SourceLocation(artifact_id="wf-1", json_pointer="/nodes/0/name")
    rename = Change(
        change_id="change-a",
        node_id="a",
        kind="UNKNOWN",
        before_value="A",
        after_value="A2",
        before_source=name,
        after_source=name,
        extensions={
            NAME_REFERENCES: {
                "status": "BROKEN_REFERENCES",
                "references": [
                    {"side": "after", "node_id": "b", "name": "A", "pointer": pointer}
                    for pointer in pointers
                ],
                "related_change_ids": [],
            }
        },
    )
    built: list[str] = []
    original = impact._candidate

    def counting(route: impact.Route, search: Any, end: str) -> impact.Candidate:
        built.append(end)
        return original(route, search, end)

    monkeypatch.setattr(impact, "_candidate", counting)
    graphs: dict[impact.Side, SemanticGraph] = {"before": before, "after": after}
    facts: dict[impact.Side, dict[str, Any]] = {"before": {}, "after": {}}
    result = impact.impact_paths([rename], graphs, facts, "task-1", 1)
    assert len(result.paths) == 1
    assert result.complete is False
    # The renamed node itself (0 edges) is kept; one reference path shows there are more.
    # The other 99 reference routes are never built.
    assert len(built) == 2
    everything = impact.impact_paths([rename], graphs, facts, "task-1")
    assert len(everything.paths) == 101


@pytest.mark.parametrize("field", ["max_workflow_nodes", "max_snapshot_nodes", "max_impact_paths"])
def test_negative_limits_are_refused(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        AnalysisLimits(**{field: -1})
    with pytest.raises(ValueError, match="non-negative"):
        impact.impact_paths([], {}, {}, "task-1", -1)


def test_paths_past_the_limit_are_never_built(monkeypatch: pytest.MonkeyPatch) -> None:
    handoff = with_renamed_split(with_priority_threshold(load("support-triage"), 4))
    built: list[str] = []
    original = impact._path

    def counting(*args: Any, **kwargs: Any) -> ImpactPath:
        path = original(*args, **kwargs)
        built.append(path.path_id)
        return path

    monkeypatch.setattr(impact, "_path", counting)
    result = analyze(handoff, AnalysisLimits(max_impact_paths=2))
    # Nine paths exist; only the two kept ones are generated.
    assert sorted(built) == sorted(path.path_id for path in result.impact_paths)
    assert len(built) == 2


def test_workflow_node_limit_skips_field_flow_and_impact_paths() -> None:
    result = checked("order-lookup", load("order-lookup"), AnalysisLimits(max_workflow_nodes=2))
    assert record(result)["truncated"] is True
    assert record(result)["reasons"] == [
        {"code": "WORKFLOW_NODE_LIMIT", "scope": "before", "count": 3, "limit": 2},
        {"code": "WORKFLOW_NODE_LIMIT", "scope": "after", "count": 3, "limit": 2},
    ]
    assert record(result)["impact_paths"] == {
        "computed": False,
        "complete": False,
        "total": None,
        "kept": 0,
    }
    assert result.impact_paths == []
    for graph in (result.before_graph, result.after_graph):
        assert field_flows(graph) == []
        assert len([edge for edge in graph.edges if edge.relation == "CONTROL"]) == 2
        assert any("필드 흐름" in item and "상한" in item for item in graph.limitations)
    assert len(result.changes) == 1
    assert any("영향 경로를 계산하지 않았다" in item for item in result.limitations)


def test_snapshot_node_limit_counts_fixed_sub_workflows() -> None:
    # 9 nodes in each workflow plus 2 in the shared summary sub-workflow.
    result = checked(
        "support-triage", load("support-triage"), AnalysisLimits(max_snapshot_nodes=10)
    )
    assert record(result)["reasons"] == [
        {"code": "SNAPSHOT_NODE_LIMIT", "scope": "before", "count": 11, "limit": 10},
        {"code": "SNAPSHOT_NODE_LIMIT", "scope": "after", "count": 11, "limit": 10},
    ]
    assert result.impact_paths == []
    assert field_flows(result.dependency_graphs["summary"]) == []
    # Name-reference DATA edges are part of the base graph and stay.
    names = {
        (edge.source_node_id, edge.target_node_id)
        for edge in result.after_graph.edges
        if edge.extensions.get(DATA_ORIGIN) == "NAME_REFERENCE"
    }
    assert names == {("split", "urgent"), ("split", "standard")}
