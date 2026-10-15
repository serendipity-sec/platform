import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.contracts import Handoff, NormalizedNode, NormalizedWorkflow
from flowdelta.graph import analyze
from flowdelta.ingestion import InputError, json_bytes
from flowdelta.pipeline_contracts import AnalysisBundle, Change
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline

ROOT = Path(__file__).resolve().parents[2]
REFERENCES = {
    "order-lookup": ROOT / "examples/contracts/order-lookup",
    "support-triage": ROOT / "examples/contracts/support-triage",
}


def load(case: str) -> Handoff:
    return read_handoff(REFERENCES[case] / "input")


def edge_keys(bundle: AnalysisBundle, side: str) -> list[tuple[object, ...]]:
    graph = bundle.before_graph if side == "before" else bundle.after_graph
    return sorted(
        (
            edge.source_node_id,
            edge.target_node_id,
            edge.output_type,
            edge.output_port,
            edge.input_type,
            edge.input_port,
            edge.condition is not None,
        )
        for edge in graph.edges
    )


def change_keys(changes: list[Change]) -> list[tuple[object, ...]]:
    return sorted(
        (
            change.kind,
            change.node_id,
            change.before_source.json_pointer if change.before_source else None,
            change.after_source.json_pointer if change.after_source else None,
            json.dumps(change.before_value, sort_keys=True),
            json.dumps(change.after_value, sort_keys=True),
        )
        for change in changes
    )


def with_after(handoff: Handoff, after: NormalizedWorkflow) -> Handoff:
    return handoff.model_copy(update={"after": after})


def replace_node(
    workflow: NormalizedWorkflow, node_id: str, **update: object
) -> NormalizedWorkflow:
    nodes = [
        node.model_copy(update=update) if node.node_id == node_id else node
        for node in workflow.nodes
    ]
    return workflow.model_copy(update={"nodes": nodes})


def kinds(bundle: AnalysisBundle) -> list[str]:
    return sorted(change.kind for change in bundle.changes)


@pytest.mark.parametrize("case", sorted(REFERENCES))
def test_generated_analysis_matches_reference_structure(case: str) -> None:
    reference = read_pipeline_examples(REFERENCES[case]).analysis
    handoff = load(case)
    generated = analyze(handoff)

    assert generated.authorship.origin == "MODULE_OUTPUT"
    assert reference.authorship.origin == "CONTRACT_EXAMPLE"
    assert generated.case_id == reference.case_id
    assert (generated.before_snapshot_id, generated.after_snapshot_id) == (
        reference.before_snapshot_id,
        reference.after_snapshot_id,
    )
    assert [(m.before_node_id, m.after_node_id, m.method) for m in generated.node_matches] == [
        (m.before_node_id, m.after_node_id, m.method) for m in reference.node_matches
    ]
    for side in ["before", "after"]:
        assert edge_keys(generated, side) == edge_keys(reference, side)
    assert change_keys(generated.changes) == change_keys(reference.changes)
    assert (generated.task_ref, generated.policy_ref) == (reference.task_ref, reference.policy_ref)
    assert set(generated.obligation_ids) == set(reference.obligation_ids)
    assert generated.limitations


@pytest.mark.parametrize("case", sorted(REFERENCES))
def test_generated_analysis_passes_pipeline_contract(case: str) -> None:
    examples = read_pipeline_examples(REFERENCES[case])
    handoff = load(case)
    generated = analyze(handoff)
    # B's plan is still a CONTRACT_EXAMPLE; only its analysis reference is pointed at A's output.
    plan = examples.plan.model_copy(update={"analysis_id": generated.analysis_id})
    validate_pipeline(handoff, generated, examples.catalog, plan)


def test_analysis_is_deterministic() -> None:
    first = json_bytes(analyze(load("order-lookup")).model_dump(mode="json"))
    second = json_bytes(analyze(load("order-lookup")).model_dump(mode="json"))
    assert first == second


def test_position_only_change_is_display_change() -> None:
    handoff = load("order-lookup")
    after = replace_node(
        handoff.before.model_copy(update={"source_artifact_id": handoff.after.source_artifact_id}),
        "request",
        opaque_fields={**handoff.before.nodes[0].opaque_fields, "position": [999, 999]},
    )
    result = analyze(with_after(handoff, after))
    assert kinds(result) == ["DISPLAY_CHANGED"]
    assert result.changes[0].after_source is not None
    assert result.changes[0].after_source.json_pointer == "/nodes/0/position"


def test_settings_credentials_version_and_name_are_classified() -> None:
    handoff = load("order-lookup")
    request = handoff.after.nodes[0]
    credentials = {"httpHeaderAuth": {"id": "other-auth", "name": "Other"}}
    after = replace_node(
        handoff.after,
        "request",
        name="Renamed request",
        type_version=2.1,
        opaque_fields={**request.opaque_fields, "credentials": credentials, "onError": "x"},
    )
    result = analyze(with_after(handoff, after))
    assert kinds(result) == [
        "CREDENTIAL_CHANGED",
        "PARAMETER_CHANGED",
        "SETTINGS_CHANGED",
        "TYPE_VERSION_CHANGED",
        "UNKNOWN",
    ]
    name_change = next(change for change in result.changes if change.kind == "UNKNOWN")
    assert (name_change.before_value, name_change.after_value) == (
        "Order request",
        "Renamed request",
    )


def test_added_and_removed_nodes_are_not_guessed() -> None:
    handoff = load("order-lookup")
    response = handoff.after.nodes[2]
    renamed = response.model_copy(update={"node_id": "response-v2"})
    connections = [
        connection.model_copy(update={"target_node_id": "response-v2"})
        if connection.target_node_id == "response"
        else connection
        for connection in handoff.after.connections
    ]
    after = handoff.after.model_copy(
        update={"nodes": [*handoff.after.nodes[:2], renamed], "connections": connections}
    )
    result = analyze(with_after(handoff, after))
    methods = {(m.before_node_id, m.after_node_id): m.method for m in result.node_matches}
    assert methods[("response", None)] == "REMOVED"
    assert methods[(None, "response-v2")] == "ADDED"
    assert kinds(result) == ["ADDED", "CONNECTION_CHANGED", "CONNECTION_CHANGED", "REMOVED"]


def test_other_ids_and_parameters_are_not_hardcoded() -> None:
    handoff = load("order-lookup")
    ids = {"request": "n1", "read-order": "n2", "response": "n3"}

    def rename(workflow: NormalizedWorkflow) -> NormalizedWorkflow:
        nodes = [node.model_copy(update={"node_id": ids[node.node_id]}) for node in workflow.nodes]
        connections = [
            connection.model_copy(
                update={
                    "source_node_id": ids[connection.source_node_id],
                    "target_node_id": ids[connection.target_node_id],
                }
            )
            for connection in workflow.connections
        ]
        return workflow.model_copy(update={"nodes": nodes, "connections": connections})

    before = rename(handoff.before)
    after = replace_node(rename(handoff.after), "n2", parameters={"url": "http://other"})
    result = analyze(handoff.model_copy(update={"before": before, "after": after}))
    assert {m.before_node_id for m in result.node_matches} == {"n1", "n2", "n3"}
    changed = {(change.node_id, change.kind) for change in result.changes}
    assert ("n3", "PARAMETER_CHANGED") in changed
    assert ("n2", "PARAMETER_CHANGED") in changed


def test_list_items_with_ids_are_matched_by_id() -> None:
    handoff = load("support-triage")
    result_node = next(node for node in handoff.after.nodes if node.node_id == "result")
    items = result_node.parameters["assignments"]["assignments"]  # type: ignore[index]
    inserted = [items[0], {"id": "middle", "name": "x", "type": "string", "value": "y"}, *items[1:]]
    parameters = {**result_node.parameters, "assignments": {"assignments": inserted}}
    after = replace_node(handoff.after, "result", parameters=parameters)
    result = analyze(with_after(handoff, after))
    added = [change for change in result.changes if change.before_source is None]
    assert len(added) == 2
    assert {change.after_source.json_pointer for change in added if change.after_source} == {
        "/nodes/8/parameters/assignments/assignments/1",
        "/nodes/8/parameters/assignments/assignments/3",
    }


def test_dangling_connection_and_duplicate_node_are_errors() -> None:
    handoff = load("order-lookup")
    dangling = handoff.after.connections[0].model_copy(update={"target_node_id": "missing"})
    after = handoff.after.model_copy(update={"connections": [dangling]})
    with pytest.raises(InputError) as error:
        analyze(with_after(handoff, after))
    assert error.value.code == "GRAPH_DANGLING_CONNECTION"

    duplicate: list[NormalizedNode] = [*handoff.after.nodes, handoff.after.nodes[0]]
    after = handoff.after.model_copy(update={"nodes": duplicate})
    with pytest.raises(InputError) as error:
        analyze(with_after(handoff, after))
    assert error.value.code == "GRAPH_DUPLICATE_NODE_ID"


def test_reordering_identified_parameter_items_is_not_lost() -> None:
    handoff = load("support-triage")
    node = next(node for node in handoff.after.nodes if node.node_id == "result")
    items = node.parameters["assignments"]["assignments"]  # type: ignore[index]
    parameters = {**node.parameters, "assignments": {"assignments": list(reversed(items))}}
    before = handoff.after
    after = replace_node(before, "result", parameters=parameters)
    result = analyze(handoff.model_copy(update={"before": before, "after": after}))
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind == "PARAMETER_CHANGED"
    assert change.before_value == items
    assert change.after_value == list(reversed(items))
    assert change.after_source is not None
    assert change.after_source.json_pointer == "/nodes/8/parameters/assignments/assignments"


def test_cli_writes_new_analysis_file_once(tmp_path: Path) -> None:
    out = tmp_path / "analysis.json"
    runner = CliRunner()
    command = ["analyze", str(REFERENCES["order-lookup"] / "input"), "--out", str(out)]
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    written = AnalysisBundle.model_validate_json(out.read_bytes())
    assert written.authorship.origin == "MODULE_OUTPUT"
    second = runner.invoke(app, command)
    assert second.exit_code == 2
    assert "OUTPUT_EXISTS" in second.output
