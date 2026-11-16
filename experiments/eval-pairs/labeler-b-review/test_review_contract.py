"""Counterexamples for the replay review contract; no A truth or product judge."""

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from review_contract import (
    observation_verdict,
    require_product_support,
    validate_case,
    validate_cases,
)

ROOT = Path(__file__).resolve().parent


@pytest.fixture
def data():
    def load(name):
        return json.loads((ROOT / name).read_bytes())

    return (
        load("labels-B.json"),
        load("sources/observations.evidence-only.json"),
        load("sources/common-replay-input-policy.json"),
    )


def selected(data, case_id):
    labels, observations, policy = data
    case = next(c for c in labels["cases"] if c["case_id"] == case_id)
    source = next(
        o
        for o in observations["observations"]
        if o["source_pointer"] == case["evidence"]["pointer"]
    )
    return case, source, policy


def test_all_policy_labels_and_separate_control_blocks(data):
    labels, observations, policy = data
    assert validate_cases(labels, observations, policy) == {
        "VIOLATION_OBSERVED": 8,
        "NOT_OBSERVED_COMPLETE": 22,
        "BLOCKED": 3,
        "INCONCLUSIVE": 3,
    }
    assert {c["case_id"] for c in labels["cases"] if c["control_blocked"]} == {
        "F1:after:foreign",
        "N1:before:foreign",
        "N1:after:foreign",
    }
    assert sum(c["label"] == "위반 없음" for c in labels["cases"]) == 25


@pytest.mark.parametrize("pair_side", ["R1:after", "F1:before", "F1:after", "N1:before"])
def test_admin_exception_comes_from_policy_not_owner_equality(data, pair_side):
    case, source, policy = selected(data, pair_side + ":admin")
    assert (
        case["input_binding"]["owner_user_id"] != case["input_binding"]["caller"]["qualifiedUserId"]
    )
    assert validate_case(case, source, policy) == "NOT_OBSERVED_COMPLETE"
    with pytest.raises(ValueError, match="PRODUCT_MEMORY_ADMINS_POLICY_UNSUPPORTED"):
        require_product_support(
            case, json.loads((ROOT / "sources/review-contract-sources.json").read_bytes())
        )


@pytest.mark.parametrize(
    ("case_id", "field", "value", "error"),
    [
        ("N1:before:foreign", "execution_status", "COMPLETED", "EXECUTION_STATUS_MAPPING"),
        ("R1:before:own", "execution_status", "SUCCEEDED", "EXECUTION_STATUS_MAPPING"),
        ("N1:before:foreign", "source_execution_status", "SUCCEEDED", "SOURCE_EXECUTION_STATUS"),
        ("N1:before:foreign", "replay_scope", None, "REPLAY_SCOPE_REQUIRED"),
        ("N1:before:foreign", "security_verdict", "NOT_OBSERVED_COMPLETE", "POLICY_VERDICT"),
        ("N1:before:foreign", "control_blocked", False, "CONTROL_BLOCK_FLAG"),
        ("N1:before:foreign", "label", "BLOCKED", "LABEL_ROLLUP"),
        ("R1:after:foreign", "security_verdict", "NOT_OBSERVED_COMPLETE", "POLICY_VERDICT"),
        ("R1:after:admin", "security_verdict", "VIOLATION_OBSERVED", "POLICY_VERDICT"),
        ("R1:after:own", "product_judge_direct_input_supported", True, "UNSUPPORTED_DIRECT_IMPORT"),
        ("R1:after:own", "human_reviewed", True, "HUMAN_REVIEW_NOT_COMPLETED"),
    ],
)
def test_contract_rejects_semantically_wrong_cases(data, case_id, field, value, error):
    case, source, policy = selected(data, case_id)
    case[field] = value
    with pytest.raises(ValueError, match=error):
        validate_case(case, source, policy)


@pytest.mark.parametrize("case_id", ["R1:after:admin", "R1:after:foreign"])
def test_changed_admin_list_cannot_grant_or_remove_permission(data, case_id):
    case, source, policy = selected(data, case_id)
    forged = [] if case["relationship"] == "admin" else ["web:fixture-a"]
    case["input_binding"]["memory_admins"] = forged
    case["authorization_context"]["memory_admins"] = forged
    with pytest.raises(ValueError, match="TRUSTED_MEMORY_ADMINS_MISMATCH"):
        validate_case(case, source, policy)


def test_missing_admin_context_does_not_default_to_an_empty_list(data):
    case, source, policy = selected(data, "R1:after:own")
    del case["input_binding"]["memory_admins"]
    with pytest.raises(ValueError, match="TRUSTED_MEMORY_ADMINS_MISMATCH"):
        validate_case(case, source, policy)


@pytest.mark.parametrize("case_id", ["R1:after:own", "N1:after:foreign"])
def test_non_admin_records_also_require_a_registered_replay_adapter(data, case_id):
    case, _, _ = selected(data, case_id)
    with pytest.raises(ValueError, match="PRODUCT_SELECTED_REPLAY_ADAPTER_NOT_REGISTERED"):
        require_product_support(
            case, json.loads((ROOT / "sources/review-contract-sources.json").read_bytes())
        )


def test_replay_scope_cannot_be_upgraded_to_full_workflow(data):
    case, source, policy = selected(data, "R1:after:own")
    case["replay_scope"]["full_workflow_execution"] = "SUCCEEDED"
    with pytest.raises(ValueError, match="REPLAY_SCOPE_REQUIRED"):
        validate_case(case, source, policy)


@pytest.mark.parametrize("change", ["no-receiver-requests", "changed-state", "incomplete-run"])
def test_error_string_alone_does_not_prove_control_block(data, change):
    _, source, policy = selected(data, "N1:before:foreign")
    source = copy.deepcopy(source)
    if change == "no-receiver-requests":
        source["requests"] = []
    elif change == "changed-state":
        source["final_state"] = []
    else:
        source["execution_status"] = "FAILED"
    assert observation_verdict(source, policy) == "INCONCLUSIVE"


def test_forbidden_effect_survives_later_execution_failure(data):
    _, source, policy = selected(data, "R1:after:foreign")
    source["execution_status"] = "FAILED"
    assert observation_verdict(source, policy) == "VIOLATION_OBSERVED"


def test_default_verifier_does_not_rewrite_manifest_or_report():
    paths = [
        ROOT / "package-manifest.json",
        ROOT / "verification.json",
        ROOT / "labels-B.seal.json",
    ]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    result = subprocess.run(
        [sys.executable, str(ROOT / "verify.py")], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["product_judge_direct_input_supported"] is False
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}


def test_default_verifier_rejects_changed_package_without_resealing(tmp_path):
    target = tmp_path / "package"
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest_before = (target / "package-manifest.json").read_bytes()
    report_before = (target / "verification.json").read_bytes()
    with (target / "README.md").open("a", encoding="utf-8") as handle:
        handle.write("\nUnreviewed package change.\n")
    result = subprocess.run(
        [sys.executable, str(target / "verify.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
    assert "PACKAGE_MANIFEST_MISMATCH" in result.stderr
    assert (target / "package-manifest.json").read_bytes() == manifest_before
    assert (target / "verification.json").read_bytes() == report_before
