import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.contracts import Policy, PolicyBindings
from flowdelta.fixed_profile import freeze_profile, validate_profile
from flowdelta.graph import analyze
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, load_case, parse_model
from flowdelta.input_repository import InputRecord
from flowdelta.knowledge import load_cards, make_plan
from flowdelta.normalize import normalize
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline
from flowdelta.policy_bindings import resolve_policy_bindings, validate_policy_bindings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/policy-bindings-v1"
DATA = json.loads((FIXTURES / "fixtures.json").read_bytes())


def with_binding(bundle: Path) -> None:
    bindings = json.loads((FIXTURES / "order-bindings.json").read_bytes())
    edit_document(bundle, "policy.json", lambda policy: policy.update(bindings=bindings))


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda item: item["name"])
def test_shared_reference_fixtures(case) -> None:
    policy = parse_model(Policy, case["policy"])
    before = normalize(DATA["before"], "before")
    after = normalize(DATA["after"], "after")
    if case["expected_error"]:
        with pytest.raises(InputError, match=case["expected_error"]):
            validate_policy_bindings(policy, before, after)
    else:
        resolved = validate_policy_bindings(policy, before, after)
        assert len(resolved.targets) == 1
        assert len(resolved.limitations) == case["expected_note_count"]
        assert resolved.targets[0].rule_id == "memory-owner-delete"


def test_legacy_absence_preserves_serialization_and_two_reference_bundles() -> None:
    for name in ("order-lookup", "support-triage"):
        read_pipeline_examples(ROOT / "examples/contracts" / name)
        handoff = read_handoff(ROOT / "examples/contracts" / name / "input")
        assert "bindings" not in handoff.policy.model_dump(mode="json")
        assert resolve_policy_bindings(handoff).targets == ()


def test_bound_policy_survives_source_snapshot_and_handoff(bundle: Path, tmp_path: Path) -> None:
    legacy = build_handoff(load_case(bundle))
    with_binding(bundle)
    loaded = load_case(bundle)
    handoff = build_handoff(loaded)
    write_handoff(loaded, handoff, tmp_path / "bound-handoff")
    actual = read_handoff(tmp_path / "bound-handoff")
    assert actual.policy == handoff.policy
    assert actual.policy.bindings.version == "1.0.0"
    assert actual.snapshots != legacy.snapshots
    assert resolve_policy_bindings(actual).targets[0].node_id == "response"
    # Input resolution alone does not pretend #61 is already wired into main's analyze().
    assert all(not path.policy_rule_ids for path in analyze(actual).impact_paths)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda b: b.update(rule_id="absent"), "POLICY_TARGET_RULE_UNKNOWN"),
        (lambda b: b["applies_to"][0].update(node_id="absent"), "POLICY_TARGET_NODE_UNKNOWN"),
        (lambda b: b["subject"].update(node_id="absent"), "POLICY_BINDING_SUBJECT_UNKNOWN"),
        (
            lambda b: b["applies_to"][0].update(json_pointer="/nodes/1/parameters"),
            "POLICY_BINDING_POINTER_MISMATCH",
        ),
        (
            lambda b: b["applies_to"][0].update(json_pointer="/nodes/2/parameters/absent"),
            "POLICY_BINDING_POINTER_MISMATCH",
        ),
    ],
)
def test_loader_rejects_invalid_references(bundle: Path, mutation, code: str) -> None:
    with_binding(bundle)
    edit_document(bundle, "policy.json", lambda p: mutation(p["bindings"]["bindings"][0]))
    with pytest.raises(InputError, match=code):
        load_case(bundle)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["bindings"].update(version="2.0.0"),
        lambda p: p["bindings"]["bindings"][0].update(policy_kind="P3"),
        lambda p: p["bindings"]["bindings"][0].pop("allowed_exceptions"),
        lambda p: p["bindings"]["bindings"][0].update(security_outcome="VIOLATION"),
        lambda p: p["bindings"]["bindings"][0]["applies_to"][0].update(side="both"),
        lambda p: p["bindings"]["bindings"][0].update(evidence_refs=[]),
        lambda p: p["bindings"]["bindings"].append(p["bindings"]["bindings"][0]),
    ],
)
def test_unknown_version_kind_verdict_and_incomplete_shape_refused(mutate) -> None:
    policy = copy.deepcopy(DATA["cases"][0]["policy"])
    mutate(policy)
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        parse_model(Policy, policy)


def test_admin_exception_requires_explicit_policy_basis_and_agreement() -> None:
    policy = copy.deepcopy(DATA["cases"][0]["policy"])
    evidence = copy.deepcopy(policy["bindings"]["bindings"][0]["evidence_refs"])
    evidence[0]["kind"] = "PUBLIC_IMPLEMENTATION"
    policy["bindings"]["bindings"][0]["allowed_exceptions"] = [
        {"condition": "subject_role == admin", "evidence_refs": evidence}
    ]
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        parse_model(Policy, policy)
    evidence[0]["kind"] = "RESEARCH_POLICY"
    before, after = normalize(DATA["before"], "before"), normalize(DATA["after"], "after")
    with pytest.raises(InputError, match="POLICY_BINDING_EXCEPTIONS_MISMATCH"):
        validate_policy_bindings(parse_model(Policy, policy), before, after)
    policy["rules"][0]["exceptions"] = ["subject_role == admin"]
    assert validate_policy_bindings(parse_model(Policy, policy), before, after).targets


@pytest.mark.parametrize("pointer", ["/nodes/1/parameters/a~1b/~0key/0", "/nodes/1"])
def test_escaped_pointer_and_node_pointer(pointer: str) -> None:
    after = copy.deepcopy(DATA["after"])
    after["nodes"][1]["parameters"]["a/b"] = {"~key": [None]}
    policy = copy.deepcopy(DATA["cases"][0]["policy"])
    policy["bindings"]["bindings"][0]["applies_to"][0]["json_pointer"] = pointer
    assert validate_policy_bindings(
        parse_model(Policy, policy), normalize(DATA["before"], "before"), normalize(after, "after")
    ).targets


def test_fixed_profile_rejects_policy_binding_change(bundle: Path) -> None:
    with_binding(bundle)
    profile = freeze_profile(load_case(bundle), profile_id="binding-fixture", revision="1")
    edit_document(
        bundle,
        "policy.json",
        lambda p: p["bindings"]["bindings"][0]["protected_resource"].update(fields=["status"]),
    )
    with pytest.raises(InputError):
        validate_profile(load_case(bundle), profile)


def test_handoff_cannot_substitute_normalized_pointer(bundle: Path, tmp_path: Path) -> None:
    with_binding(bundle)
    loaded = load_case(bundle)
    output = tmp_path / "bound"
    write_handoff(loaded, build_handoff(loaded), output)
    edit_document(
        output,
        "after.normalized.json",
        lambda w: w["nodes"][2]["source"].update(json_pointer="/nodes/99"),
    )
    with pytest.raises(InputError, match="POLICY_BINDING_SOURCE_MISMATCH"):
        read_handoff(output)


@pytest.mark.parametrize(
    ("field", "code"),
    [("policy_rule_ids", "IMPACT_PATH_POLICY_UNKNOWN"), ("task_ids", "IMPACT_PATH_TASK_UNKNOWN")],
)
def test_pipeline_refuses_foreign_rule_or_task(field: str, code: str) -> None:
    root = ROOT / "examples/contracts/order-lookup"
    handoff = read_handoff(root / "input")
    examples = read_pipeline_examples(root)
    analysis = analyze(handoff)
    plan = make_plan(handoff, analysis, load_cards(root / "cards"), examples.catalog)
    analysis.impact_paths[0] = analysis.impact_paths[0].model_copy(update={field: ["foreign-id"]})
    with pytest.raises(InputError, match=code):
        validate_pipeline(handoff, analysis, examples.catalog, plan)


def test_schema_artifacts_are_current() -> None:
    schema = json.loads((ROOT / "schemas/policy-bindings-v1.schema.json").read_bytes())
    assert schema == PolicyBindings.model_json_schema()


def test_schema_only_refresh_keeps_input_record_and_policy_binding(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "refresh_contract_artifacts", ROOT / "tools/refresh_contract_artifacts.py"
    )
    assert spec is not None and spec.loader is not None
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["refresh_contract_artifacts.py", "--schemas-only"])
    refresh.main()
    assert json.loads((tmp_path / "schemas/input-record-v1.schema.json").read_bytes()) == (
        InputRecord.model_json_schema()
    )
    assert json.loads((tmp_path / "schemas/policy-bindings-v1.schema.json").read_bytes()) == (
        PolicyBindings.model_json_schema()
    )
    assert {path.name for path in tmp_path.iterdir()} == {"schemas"}
