import json
import shutil
from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.additional_examples import write_support_examples
from flowdelta.conformance.compare import compare_case
from flowdelta.conformance.observations import decode_reference_table
from flowdelta.contracts import DependencyManifest
from flowdelta.dependencies import validate_manifest
from flowdelta.expressions import parse_expression
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.module_outputs import read_module_outputs, validate_runs
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "examples/contracts/support-triage"


@pytest.fixture
def support(tmp_path: Path) -> Path:
    return Path(shutil.copytree(ROOT / "examples/support-triage", tmp_path / "support"))


def test_second_business_preserves_branch_items_and_child(support: Path) -> None:
    handoff = build_handoff(load_case(support))
    assert handoff.before.semantic_support == "SUPPORTED"
    facts = {f.node_id: f for f in handoff.semantics.semantics if f.side == "before"}
    branch = next(f for f in facts.values() if f.node_type.endswith(".if"))
    assert [(b.output_index, b.when) for b in branch.branches] == [(0, True), (1, False)]
    merge = next(f for f in facts.values() if f.node_type.endswith(".merge"))
    assert merge.item_behavior.input_ports == 2
    assert merge.item_behavior.cardinality == "SUM_INPUTS"
    assert handoff.dependency_workflows["summary"].semantic_support == "SUPPORTED"
    for selection, selector in [("LINKED", "item"), ("FIRST", "first()")]:
        value = parse_expression(
            f"={{{{ $('Split tickets').{selector}.json.ticket_id }}}}"
        ).to_contract()
        assert value.item_selection == selection
        assert value.node_name == "Split tickets"


@pytest.mark.parametrize(
    "update",
    [
        lambda v: v["nodes"][1].update(retryOnFail=True),
        lambda v: v["nodes"][1]["parameters"].update(mode={"unexpected": True}),
        lambda v: v["nodes"][1].update(typeVersion=99),
    ],
)
def test_unknown_parameter_version_and_retry_are_bounded(support: Path, update) -> None:
    edit_document(support, "workflow/before.json", update)
    handoff = build_handoff(load_case(support))
    unsupported = [
        f for f in handoff.semantics.semantics if f.side == "before" and f.support == "UNSUPPORTED"
    ]
    assert unsupported
    assert all(f.effects is None and f.unsupported_locations for f in unsupported)


def test_unsupported_child_is_visible_at_parent(support: Path) -> None:
    edit_document(
        support, "dependencies/summary.json", lambda v: v["nodes"][1].update(typeVersion=99)
    )
    handoff = build_handoff(load_case(support))
    assert handoff.before.semantic_support == "PARTIAL"
    assert handoff.dependency_workflows["summary"].semantic_support == "PARTIAL"


def test_dependency_versions_may_differ_between_sides_not_within_side() -> None:
    entries = [
        {
            "dependency_id": name,
            "kind": "WORKFLOW",
            "version": version,
            "path": name + ".json",
            "workflow_id": "same-runtime-id",
            "requires": [],
        }
        for name, version in [("old", "1"), ("new", "2")]
    ]
    manifest = DependencyManifest(
        schema_version="0.4.0", dependencies=entries, before_roots=["old"], after_roots=["new"]
    )
    validate_manifest(manifest)
    manifest.before_roots.append("new")
    with pytest.raises(InputError, match="AMBIGUOUS_DEPENDENCY_WORKFLOW_ID"):
        validate_manifest(manifest)


def test_second_reference_bundle_is_reproducible(tmp_path: Path) -> None:
    generated = write_support_examples(ROOT, tmp_path / "reference")
    read_pipeline_examples(generated)
    for path in REFERENCE.rglob("*.json"):
        assert path.read_bytes() == (generated / path.relative_to(REFERENCE)).read_bytes()


@pytest.mark.parametrize("failure", ["port", "condition", "manual-input", "missing-edge"])
def test_graph_ports_conditions_and_materialized_input_remain_bound(failure: str) -> None:
    example = read_pipeline_examples(REFERENCE)
    handoff = read_handoff(REFERENCE / "input")
    edge = next(edge for edge in example.analysis.before_graph.edges if edge.condition)
    if failure == "port":
        edge.output_port = 9
    elif failure == "condition":
        edge.condition.when = not edge.condition.when
    elif failure == "missing-edge":
        example.analysis.before_graph.edges.remove(edge)
    else:
        binding = example.catalog.templates[0].materialized_input_bindings[0]
        node = next(node for node in handoff.before.nodes if node.node_id == binding.node_id)
        node.parameters["assignments"]["assignments"][0]["value"] = []
    with pytest.raises(InputError):
        validate_pipeline(handoff, example.analysis, example.catalog, example.plan)


def test_reference_authorship_cannot_stand_for_actual_module_output() -> None:
    with pytest.raises(InputError, match="MODULE_OUTPUT_REQUIRED"):
        read_module_outputs(REFERENCE)


def test_run_attempts_cannot_reuse_identity_or_skip_after() -> None:
    example = read_pipeline_examples(REFERENCE)
    handoff = read_handoff(REFERENCE / "input")
    with pytest.raises(InputError, match="RUN_ATTEMPT_COVERAGE_MISMATCH"):
        validate_runs(handoff, example.catalog, example.plan, [example.run], REFERENCE)
    other = example.run.model_copy(deep=True, update={"side": "after"})
    with pytest.raises(InputError, match="RUN_ID_REUSED"):
        validate_runs(handoff, example.catalog, example.plan, [example.run, other], REFERENCE)


def test_reference_table_and_missing_runtime_evidence(tmp_path: Path) -> None:
    assert decode_reference_table('[{"a":"1","b":"1"},"text"]') == {"a": "text", "b": "text"}
    with pytest.raises(InputError, match="REFERENCE_TABLE_INVALID"):
        decode_reference_table('[{"self":"0"}]')
    (tmp_path / "run.json").write_bytes(
        json_bytes(
            {
                "case_id": "support-before",
                "run_id": "x",
                "execution_status": "SUCCEEDED",
                "cleanup_status": "COMPLETE",
            }
        )
    )
    (tmp_path / "runtime-executions.json").write_text(json.dumps({"executions": []}))
    report = compare_case(ROOT, tmp_path)
    assert report["all_observed_checks_match"] is False
    assert any(
        c["check"] == "parent-execution-count" and c["status"] == "MISMATCH"
        for c in report["checks"]
    )
