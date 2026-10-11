from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.handoff import build_handoff
from flowdelta.ingestion import InputError, load_case


def test_connections_and_unknown_fields_are_preserved(bundle: Path) -> None:
    def update(value):
        value["futureSetting"] = {"keep": True}
        value["nodes"][0]["futureNodeFlag"] = {"keep": True}
        value["connections"]["Order request"]["main"].append(
            [{"node": "Order response", "type": "ai_tool", "index": 1, "futureEdgeFlag": 9}]
        )

    edit_document(bundle, "workflow/before.json", update)
    handoff = build_handoff(load_case(bundle))
    workflow = handoff.before
    assert workflow.opaque_fields["futureSetting"] == {"keep": True}
    assert workflow.nodes[0].opaque_fields["futureNodeFlag"] == {"keep": True}
    edge = workflow.connections[1]
    assert (edge.source_node_id, edge.target_node_id) == ("request", "response")
    assert (edge.source_output_index, edge.target_input_index) == (1, 1)
    assert edge.target_input_type == "ai_tool"
    assert edge.opaque_fields == {"futureEdgeFlag": 9}
    assert edge.source.json_pointer == "/connections/Order request/main/1/0"
    assert all(
        item.support == "UNSUPPORTED" and item.effects is None
        for item in handoff.semantics.semantics
        if item.side == "before"
    )


@pytest.mark.parametrize("field", ["name", "id"])
def test_ambiguous_identity_is_not_guessed(bundle: Path, field: str) -> None:
    def update(value):
        value["nodes"][1][field] = value["nodes"][0][field]

    edit_document(bundle, "workflow/before.json", update)
    with pytest.raises(InputError, match="AMBIGUOUS_NODE_ID_OR_NAME"):
        build_handoff(load_case(bundle))


@pytest.mark.parametrize("target", ["missing", 1])
def test_dangling_connection_is_rejected(bundle: Path, target) -> None:
    def update(value):
        value["connections"]["Order request"]["main"][0][0]["node"] = target

    edit_document(bundle, "workflow/before.json", update)
    with pytest.raises(InputError, match="UNKNOWN_CONNECTION_TARGET"):
        build_handoff(load_case(bundle))


@pytest.mark.parametrize("index", [-1, True, "0"])
def test_invalid_port_index_is_not_coerced(bundle: Path, index) -> None:
    def update(value):
        value["connections"]["Order request"]["main"][0][0]["index"] = index

    edit_document(bundle, "workflow/before.json", update)
    with pytest.raises(InputError, match="INVALID_PORT_INDEX"):
        build_handoff(load_case(bundle))


def test_snapshot_pins_policy_bytes_and_declared_configuration(bundle: Path) -> None:
    first = build_handoff(load_case(bundle))
    repeated = build_handoff(load_case(bundle))
    assert first == repeated
    edit_document(bundle, "policy.json", lambda value: value.update(policy_version="2"))
    updated = build_handoff(load_case(bundle))
    for original, changed in zip(
        first.snapshots.snapshots, updated.snapshots.snapshots, strict=True
    ):
        assert original.snapshot_id != changed.snapshot_id
        assert original.raw_sha256 == changed.raw_sha256
        assert changed.semantic_sha256 is not None
        assert changed.semantic_sha256 != original.semantic_sha256
        assert changed.execution_status == "NOT_RUN"
        assert changed.dependency_status == "DECLARED_RESOLVED"


def test_coordinates_are_retained_outside_declared_configuration_hash(bundle: Path) -> None:
    original = build_handoff(load_case(bundle))

    def update(value):
        value["nodes"][0]["position"] = [120, 240]

    edit_document(bundle, "workflow/before.json", update)
    changed = build_handoff(load_case(bundle))
    assert changed.before.nodes[0].opaque_fields["position"] == [120, 240]
    assert (
        changed.snapshots.snapshots[0].structural_sha256
        != original.snapshots.snapshots[0].structural_sha256
    )
    assert (
        changed.snapshots.snapshots[0].semantic_sha256
        == original.snapshots.snapshots[0].semantic_sha256
    )
