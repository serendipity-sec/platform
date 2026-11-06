import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.contracts import Handoff
from flowdelta.graph import AnalysisLimits, analyze
from flowdelta.graph.analyze import ANALYSIS_LIMITATIONS
from flowdelta.graph.impact import POLICY_NOTE
from flowdelta.graph.policy import PolicyTarget, link_policies
from flowdelta.graph.repository import FileAnalysisRepository
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import InputError, load_case
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


BINDINGS = ROOT / "examples/policy-bindings-v1/order-bindings.json"


def bound(bundle: Path) -> Handoff:
    """The order example with the shared OBJECT_OWNERSHIP binding (own-order on after/response)."""
    edit_document(
        bundle,
        "policy.json",
        lambda policy: policy.update(bindings=json.loads(BINDINGS.read_bytes())),
    )
    return build_handoff(load_case(bundle))


def test_analyze_links_the_bound_rule_and_says_so(bundle: Path) -> None:
    unbound = analyze(build_handoff(load_case(bundle)))
    result = analyze(bound(bundle))
    [path] = result.impact_paths
    assert path.policy_rule_ids == ["own-order"]
    assert POLICY_NOTE not in path.limitations
    assert not any("정책 규칙과는 연결하지 않았다" in item for item in result.limitations)
    assert any("명시적 binding" in item for item in result.limitations)
    # The linked content gets its own ID.
    assert result.analysis_id != unbound.analysis_id


def test_unbound_input_keeps_the_earlier_output() -> None:
    result = analyze(load())
    assert result.limitations[: len(ANALYSIS_LIMITATIONS)] == ANALYSIS_LIMITATIONS
    assert all(path.policy_rule_ids == [] for path in result.impact_paths)
    assert all(POLICY_NOTE in path.limitations for path in result.impact_paths)


def test_target_missing_on_its_side_is_noted_once(bundle: Path) -> None:
    handoff = bound(bundle)
    binding = handoff.policy.bindings.bindings[0]  # type: ignore[union-attr]
    target = binding.applies_to[0].model_copy(update={"side": "before", "json_pointer": None})
    renamed = renamed_response(handoff)
    # response exists only in before now; the rule targets it on after (missing there).
    after_target = target.model_copy(update={"side": "after"})
    policy = handoff.policy.model_copy(
        update={
            "bindings": handoff.policy.bindings.model_copy(  # type: ignore[union-attr]
                update={"bindings": [binding.model_copy(update={"applies_to": [after_target]})]}
            )
        }
    )
    result = analyze(renamed.model_copy(update={"policy": policy}))
    note = "규칙 own-order의 대상 노드 response가 after workflow에 없어 경로에 연결하지 않았다."
    assert result.limitations.count(note) == 1
    assert all(path.policy_rule_ids == [] for path in result.impact_paths)


def test_references_are_checked_when_limits_skip_paths(bundle: Path) -> None:
    handoff = bound(bundle)
    binding = handoff.policy.bindings.bindings[0]  # type: ignore[union-attr]
    wrong = binding.model_copy(
        update={"applies_to": [binding.applies_to[0].model_copy(update={"node_id": "absent"})]}
    )
    policy = handoff.policy.model_copy(
        update={
            "bindings": handoff.policy.bindings.model_copy(update={"bindings": [wrong]})  # type: ignore[union-attr]
        }
    )
    with pytest.raises(InputError) as error:
        analyze(handoff.model_copy(update={"policy": policy}), AnalysisLimits(max_workflow_nodes=1))
    assert error.value.code == "POLICY_TARGET_NODE_UNKNOWN"


def test_public_analysis_integration_command_preserves_policy_provenance(tmp_path: Path) -> None:
    output = tmp_path / "policy-integration"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verify_policy_binding_integration.py"),
            "--out",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "result.json").read_bytes())
    assert report["scope"] == "LOCAL_ANALYZE_FILE_REPOSITORY_B_PLAN"
    assert report["linked_policy_rule_ids"] == ["own-order"]
    assert report["security_outcome"] is None
    stored = FileAnalysisRepository(output / "analysis-repository").get(report["analysis_id"])
    actual = analyze(read_handoff(output / "handoff"))
    assert stored == actual
    plan = json.loads((output / "plan.json").read_bytes())
    context = plan["planner_metadata"]["analysis_context"]
    assert context["analysis_id"] == actual.analysis_id
    assert context["analysis_content_sha256"] == report["analysis_sha256"]
    assert context["impact_paths"][0]["policy_rule_ids"] == ["own-order"]
