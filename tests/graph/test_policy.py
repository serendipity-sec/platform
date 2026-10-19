from pathlib import Path

import pytest

from flowdelta.contracts import Handoff
from flowdelta.graph import analyze
from flowdelta.graph.impact import POLICY_NOTE
from flowdelta.graph.policy import PolicyTarget, link_policies
from flowdelta.ingestion import InputError
from flowdelta.pipeline_contracts import AnalysisBundle
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2]
RULES = {"own-order", "read-only"}


def load() -> Handoff:
    return read_handoff(ROOT / "examples/contracts/order-lookup/input")


def link(result: AnalysisBundle, targets: list[PolicyTarget]) -> tuple[AnalysisBundle, list[str]]:
    paths, notes = link_policies(
        result.impact_paths,
        result.changes,
        {"before": result.before_graph, "after": result.after_graph},
        targets,
        RULES,
    )
    return result.model_copy(update={"impact_paths": paths}), notes


def renamed_response(handoff: Handoff) -> Handoff:
    """after replaces node `response` by `response-v2`, so it exists on one side only."""
    nodes = [
        node.model_copy(update={"node_id": "response-v2"}) if node.node_id == "response" else node
        for node in handoff.after.nodes
    ]
    connections = [
        item.model_copy(update={"target_node_id": "response-v2"})
        if item.target_node_id == "response"
        else item
        for item in handoff.after.connections
    ]
    after = handoff.after.model_copy(update={"nodes": nodes, "connections": connections})
    return handoff.model_copy(update={"after": after})


def test_rule_on_a_node_of_the_path_is_linked() -> None:
    result, notes = link(analyze(load()), [PolicyTarget("read-only", "after", "response")])
    [path] = result.impact_paths
    assert path.policy_rule_ids == ["read-only"]
    assert POLICY_NOTE not in path.limitations
    assert notes == []


def test_rule_for_the_other_side_is_not_linked() -> None:
    result, _ = link(analyze(load()), [PolicyTarget("read-only", "before", "response")])
    [path] = result.impact_paths
    assert path.side == "after"
    assert path.policy_rule_ids == []
    assert POLICY_NOTE in path.limitations


def test_rule_off_the_path_is_not_linked() -> None:
    result, _ = link(analyze(load()), [PolicyTarget("own-order", "after", "read-order")])
    [path] = result.impact_paths
    # The response change's path is response itself; read-order is upstream of it.
    assert path.policy_rule_ids == []


def test_target_missing_on_its_side_is_reported_not_guessed() -> None:
    result = analyze(renamed_response(load()))
    linked, notes = link(result, [PolicyTarget("read-only", "before", "response-v2")])
    assert all(path.policy_rule_ids == [] for path in linked.impact_paths)
    assert notes == [
        "규칙 read-only의 대상 노드 response-v2가 before workflow에 없어 경로에 연결하지 않았다."
    ]


def test_target_on_an_added_node_links_its_after_path() -> None:
    result = analyze(renamed_response(load()))
    linked, _ = link(result, [PolicyTarget("read-only", "after", "response-v2")])
    added = next(change for change in result.changes if change.kind == "ADDED")
    paths = [path for path in linked.impact_paths if added.change_id in path.change_ids]
    assert paths and all(path.policy_rule_ids == ["read-only"] for path in paths)


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (PolicyTarget("read-only", "after", "nope"), "POLICY_TARGET_NODE_UNKNOWN"),
        (PolicyTarget("other-case-rule", "after", "response"), "POLICY_TARGET_RULE_UNKNOWN"),
    ],
)
def test_invalid_targets_are_refused(target: PolicyTarget, code: str) -> None:
    with pytest.raises(InputError) as error:
        link(analyze(load()), [target])
    assert error.value.code == code
