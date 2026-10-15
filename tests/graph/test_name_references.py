from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from flowdelta.contracts import Handoff, NormalizedWorkflow
from flowdelta.graph import analyze
from flowdelta.pipeline_contracts import AnalysisBundle, Change
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = "flowdelta.graph.name_references"


def load(case: str) -> Handoff:
    return read_handoff(ROOT / "examples/contracts" / case / "input")


def rename_node(workflow: NormalizedWorkflow, node_id: str, name: str) -> NormalizedWorkflow:
    nodes = [
        node.model_copy(update={"name": name}) if node.node_id == node_id else node
        for node in workflow.nodes
    ]
    return workflow.model_copy(update={"nodes": nodes})


def replace_text(value: JsonValue, old: str, new: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [replace_text(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_text(item, old, new) for key, item in value.items()}
    return value


def rewrite_references(workflow: NormalizedWorkflow, old: str, new: str) -> NormalizedWorkflow:
    nodes = []
    for node in workflow.nodes:
        parameters = replace_text(node.parameters, old, new)
        assert isinstance(parameters, dict)
        nodes.append(node.model_copy(update={"parameters": parameters}))
    return workflow.model_copy(update={"nodes": nodes})


def rename_change(bundle: AnalysisBundle, node_id: str) -> Change:
    return next(
        change
        for change in bundle.changes
        if change.node_id == node_id
        and change.before_source is not None
        and change.before_source.json_pointer.endswith("/name")
    )


def judgment(change: Change) -> dict[str, Any]:
    value = change.extensions[EXTENSION]
    assert isinstance(value, dict)
    return value


def data_edges(bundle: AnalysisBundle, side: str) -> set[tuple[str, str]]:
    graph = bundle.before_graph if side == "before" else bundle.after_graph
    return {
        (edge.source_node_id, edge.target_node_id)
        for edge in graph.edges
        if edge.relation == "DATA"
    }


def test_name_references_become_data_edges() -> None:
    result = analyze(load("support-triage"))
    expected = {("split", "urgent"), ("split", "standard")}
    assert data_edges(result, "before") == expected
    assert data_edges(result, "after") == expected
    urgent = next(
        edge
        for edge in result.before_graph.edges
        if edge.relation == "DATA" and edge.target_node_id == "urgent"
    )
    assert urgent.basis == "STATIC_POSSIBILITY"
    assert [item.json_pointer for item in urgent.evidence] == [
        "/nodes/4/parameters/assignments/assignments/0/value"
    ]


def test_rename_with_updated_references_links_the_follow_up_changes() -> None:
    handoff = load("support-triage")
    after = rewrite_references(
        rename_node(handoff.after, "split", "Split items"), "Split tickets", "Split items"
    )
    result = analyze(handoff.model_copy(update={"after": after}))

    change = rename_change(result, "split")
    assert change.kind == "UNKNOWN"
    verdict = judgment(change)
    assert verdict["status"] == "REFERENCES_UPDATED"
    follow_ups = {
        item.change_id
        for item in result.changes
        if item.kind == "PARAMETER_CHANGED" and item.node_id in {"urgent", "standard"}
    }
    assert len(follow_ups) == 2
    assert set(verdict["related_change_ids"]) == follow_ups
    assert data_edges(result, "after") == {("split", "urgent"), ("split", "standard")}


def test_rename_without_updating_references_is_broken() -> None:
    handoff = load("support-triage")
    after = rename_node(handoff.after, "split", "Split items")
    result = analyze(handoff.model_copy(update={"after": after}))

    verdict = judgment(rename_change(result, "split"))
    assert verdict["status"] == "BROKEN_REFERENCES"
    stale = [item for item in verdict["references"] if item["side"] == "after"]
    assert {(item["node_id"], item["name"]) for item in stale} == {
        ("urgent", "Split tickets"),
        ("standard", "Split tickets"),
    }
    assert data_edges(result, "after") == set()
    assert any("Split tickets" in item for item in result.after_graph.limitations)


def test_rename_onto_another_nodes_old_name_is_retargeted() -> None:
    handoff = load("support-triage")
    swapped = rename_node(
        rename_node(handoff.after, "split", "Split items"), "seed", "Split tickets"
    )
    result = analyze(handoff.model_copy(update={"after": swapped}))

    assert judgment(rename_change(result, "split"))["status"] == "REFERENCES_RETARGETED"
    assert data_edges(result, "after") == {("seed", "urgent"), ("seed", "standard")}


def test_rename_without_references() -> None:
    handoff = load("order-lookup")
    after = rename_node(handoff.after, "request", "Incoming order")
    result = analyze(handoff.model_copy(update={"after": after}))

    verdict = judgment(rename_change(result, "request"))
    assert verdict == {"status": "NO_REFERENCES", "references": [], "related_change_ids": []}


@pytest.mark.parametrize(
    "text,names",
    [
        ("={{ $('Split tickets').item.json.id }}", ["Split tickets"]),
        ('={{ $("A b").first().json.x + $node["C"].json.y }}', ["A b", "C"]),
        ("const rows = $items('Read rows', 0); return $('Read rows').all();", ["Read rows"]),
        ("={{ $json.name }} and $('') and $(name)", []),
    ],
)
def test_reference_forms(text: str, names: list[str]) -> None:
    from flowdelta.graph.references import referenced_names

    assert referenced_names(text) == names
