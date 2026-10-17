from pathlib import Path
from typing import Any

import pytest

from flowdelta.contracts import Handoff
from flowdelta.graph import AnalysisLimits, analyze
from flowdelta.graph.control import DATA_ORIGIN
from flowdelta.ingestion import json_bytes
from flowdelta.pipeline_contracts import AnalysisBundle, SemanticGraph
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
        "impact_paths": {"computed": True, "total": 1, "kept": 1},
    }
    assert not any("상한" in item for item in result.limitations)


def test_path_limit_keeps_each_changes_shortest_path_first() -> None:
    # Two changes: result (one 0-edge path) and priority (summary in 3 edges, result in 4).
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = checked("support-triage", handoff, AnalysisLimits(max_impact_paths=2))
    assert record(result)["truncated"] is True
    assert record(result)["reasons"] == [{"code": "IMPACT_PATH_LIMIT", "count": 3, "limit": 2}]
    assert record(result)["impact_paths"] == {"computed": True, "total": 3, "kept": 2}
    kept = sorted(len(path.edge_ids) for path in result.impact_paths)
    assert kept == [0, 3]
    assert {change_id for path in result.impact_paths for change_id in path.change_ids} == {
        change.change_id for change in result.changes
    }
    assert any("영향 경로 3개 중 2개" in item for item in result.limitations)
    again = analyze(handoff, AnalysisLimits(max_impact_paths=2))
    assert json_bytes(again.model_dump(mode="json")) == json_bytes(result.model_dump(mode="json"))


def test_path_limit_below_the_change_count_reports_uncovered_changes() -> None:
    handoff = with_priority_threshold(load("support-triage"), 4)
    result = analyze(handoff, AnalysisLimits(max_impact_paths=1))
    assert len(result.impact_paths) == 1
    assert any("경로가 하나도 남지 않은 변경은 1개" in item for item in result.limitations)


def test_workflow_node_limit_skips_field_flow_and_impact_paths() -> None:
    result = checked("order-lookup", load("order-lookup"), AnalysisLimits(max_workflow_nodes=2))
    assert record(result)["truncated"] is True
    assert record(result)["reasons"] == [
        {"code": "WORKFLOW_NODE_LIMIT", "scope": "before", "count": 3, "limit": 2},
        {"code": "WORKFLOW_NODE_LIMIT", "scope": "after", "count": 3, "limit": 2},
    ]
    assert record(result)["impact_paths"] == {"computed": False, "total": None, "kept": 0}
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
