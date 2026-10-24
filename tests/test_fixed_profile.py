import json
import shutil
from pathlib import Path

import pytest
from conftest import EXAMPLE, edit_document, refresh_manifest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.fixed_profile import (
    FixedComparisonProfile,
    freeze_profile,
    load_profiled_handoff,
    profile_digest,
    read_profile,
    validate_profile,
    write_profile,
)
from flowdelta.graph import analyze
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.pipeline_io import read_handoff


def frozen(bundle: Path) -> FixedComparisonProfile:
    return freeze_profile(load_case(bundle), profile_id="test-profile", revision="1.0.0")


def add_model(bundle: Path) -> None:
    for side in ("before", "after"):
        edit_document(
            bundle,
            f"workflow/{side}.json",
            lambda w: w["nodes"].append(
                {
                    "id": "model-1",
                    "name": "Model",
                    "type": "@n8n/n8n-nodes-langchain.lmChatAnthropic",
                    "typeVersion": 1.3,
                    "parameters": {"model": "fixture-model-v1", "options": {"temperature": 0.0}},
                    "position": [0, 0],
                }
            ),
        )


@pytest.mark.parametrize("example", ["order-lookup", "support-triage"])
def test_existing_families_round_trip_and_real_a_consumption(tmp_path: Path, example: str) -> None:
    source = Path(shutil.copytree(EXAMPLE.parent / example, tmp_path / "input"))
    profile = frozen(source)
    path = tmp_path / "profile.json"
    write_profile(profile, path, input_root=source)
    pin = profile_digest(profile)
    assert read_profile(path, expected_sha256=pin) == profile
    loaded, handoff, receipt = load_profiled_handoff(source, path, expected_sha256=pin)
    assert analyze(handoff).case_id == loaded.case.case_id
    assert receipt.execution_status == "NOT_RUN"
    out = tmp_path / "handoff"
    result = CliRunner().invoke(
        app,
        [
            "profile-snapshot",
            str(source),
            "--profile",
            str(path),
            "--profile-sha256",
            pin,
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert analyze(read_handoff(out)).case_id == loaded.case.case_id


def test_candidate_id_and_provenance_change_without_rebaselining(bundle: Path) -> None:
    profile = frozen(bundle)
    before = validate_profile(load_case(bundle), profile)
    edit_document(bundle, "case.json", lambda v: v.update(case_id="candidate-002"))
    edit_document(bundle, "provenance.json", lambda v: v.update(transformations=["candidate edit"]))
    edit_document(bundle, "workflow/after.json", lambda v: v.update(name="candidate"))
    after = validate_profile(load_case(bundle), profile)
    assert before.before_snapshot_id != after.before_snapshot_id
    assert before.profile_sha256 == after.profile_sha256


@pytest.mark.parametrize(
    "path,change,code",
    [
        (
            "workflow/before.json",
            lambda v: v.update(name="rebaseline"),
            "PROFILE_FIXED_INPUT_CHANGED",
        ),
        ("task.json", lambda v: v.update(actor="different-actor"), "PROFILE_FIXED_INPUT_CHANGED"),
        ("policy.json", lambda v: v.update(policy_version="2.0"), "PROFILE_FIXED_INPUT_CHANGED"),
        (
            "environment.json",
            lambda v: v.update(target_version="9.9.9"),
            "PROFILE_FIXED_INPUT_CHANGED",
        ),
        (
            "state/initial.json",
            lambda v: v.update(data={"changed": True}),
            "PROFILE_FIXED_INPUT_CHANGED",
        ),
        ("case.json", lambda v: v.update(obligations=[]), "PROFILE_CASE_CONTRACT_CHANGED"),
        (
            "case.json",
            lambda v: v.update(family_id="another-family"),
            "PROFILE_CASE_CONTRACT_CHANGED",
        ),
        (
            "case.json",
            lambda v: v.update(changed_axes=["dependencies"]),
            "PROFILE_CHANGED_AXES_REJECTED",
        ),
        (
            "dependencies.json",
            lambda v: v["dependencies"][0].update(version="new"),
            "PROFILE_FIXED_INPUT_CHANGED",
        ),
        ("dependencies.json", lambda v: v.update(after_roots=[]), "PROFILE_DEPENDENCIES_CHANGED"),
    ],
)
def test_manifest_rehash_does_not_bypass_profile(bundle: Path, path, change, code: str) -> None:
    profile = frozen(bundle)
    edit_document(bundle, path, change)
    loaded = load_case(bundle)  # ordinary integrity check accepts these changes
    with pytest.raises(InputError, match=code):
        validate_profile(loaded, profile)


def test_dependency_content_and_roots_are_both_fixed(bundle: Path) -> None:
    profile = frozen(bundle)
    loaded = load_case(bundle)
    dependency = loaded.dependency_manifest.dependencies[0]
    edit_document(bundle, dependency.path, lambda v: v.update(added="configuration"))
    with pytest.raises(InputError, match="PROFILE_FIXED_INPUT_CHANGED"):
        validate_profile(load_case(bundle), profile)


def test_missing_dependency_manifest_is_not_empty_dependencies(bundle: Path) -> None:
    case = json.loads((bundle / "case.json").read_bytes())
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    deps = json.loads((bundle / case["dependency_manifest_ref"]).read_bytes())
    removed = {case.pop("dependency_manifest_ref"), *(d["path"] for d in deps["dependencies"])}
    manifest["artifacts"] = [r for r in manifest["artifacts"] if r["path"] not in removed]
    (bundle / "case.json").write_bytes(json_bytes(case))
    (bundle / "manifest.json").write_bytes(json_bytes(manifest))
    refresh_manifest(bundle)
    with pytest.raises(InputError, match="PROFILE_DEPENDENCIES_REQUIRED"):
        frozen(bundle)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda n: n["parameters"].update(model="another-model"),
        lambda n: n["parameters"]["options"].update(temperature=0.7),
        lambda n: n.update(credentials={"anthropicApi": {"id": "other"}}),
        lambda n: n.update(typeVersion=2.0),
        lambda n: n.update(disabled=True),
    ],
)
def test_model_settings_inside_candidate_workflow_cannot_change(bundle: Path, mutation) -> None:
    add_model(bundle)
    profile = frozen(bundle)
    edit_document(bundle, "workflow/after.json", lambda w: mutation(w["nodes"][-1]))
    with pytest.raises(InputError, match="PROFILE_MODEL_CONFIGURATION_CHANGED"):
        validate_profile(load_case(bundle), profile)


@pytest.mark.parametrize("operation", ["add", "remove", "retype"])
def test_model_inventory_changes_are_detected(bundle: Path, operation: str) -> None:
    if operation != "add":
        add_model(bundle)
    profile = frozen(bundle)
    if operation == "add":
        add_model(bundle)
        # Restore baseline so only candidate-side model inventory changes.
        edit_document(bundle, "workflow/before.json", lambda w: w["nodes"].pop())
    elif operation == "remove":
        edit_document(bundle, "workflow/after.json", lambda w: w["nodes"].pop())
    else:
        edit_document(bundle, "workflow/after.json", lambda w: w["nodes"][-1].update(type="custom"))
    with pytest.raises(InputError, match="PROFILE_MODEL_CONFIGURATION_CHANGED"):
        validate_profile(load_case(bundle), profile)


def test_dynamic_model_configuration_cannot_be_frozen(bundle: Path) -> None:
    add_model(bundle)
    edit_document(
        bundle,
        "workflow/before.json",
        lambda w: w["nodes"][-1]["parameters"].update(model="={{ $env.MODEL }}"),
    )
    with pytest.raises(InputError, match="PROFILE_DYNAMIC_MODEL_CONFIGURATION"):
        frozen(bundle)


def test_model_connections_and_custom_model_inventory_are_fixed(bundle: Path) -> None:
    add_model(bundle)
    for side in ("before", "after"):

        def connect(workflow):
            workflow["nodes"][-1]["type"] = "custom.languageModel"
            workflow["connections"]["Model"] = {
                "ai_languageModel": [
                    [{"node": workflow["nodes"][0]["name"], "type": "ai_languageModel", "index": 0}]
                ]
            }

        edit_document(bundle, f"workflow/{side}.json", connect)
    profile = frozen(bundle)
    assert "model-1" in profile.model_nodes
    edit_document(bundle, "workflow/after.json", lambda w: w["connections"].pop("Model"))
    with pytest.raises(InputError, match="PROFILE_MODEL_CONFIGURATION_CHANGED"):
        validate_profile(load_case(bundle), profile)


def test_unresolved_workflow_dependency_is_rejected(bundle: Path) -> None:
    edit_document(
        bundle,
        "workflow/after.json",
        lambda w: w.update(settings={"errorWorkflow": "not-supplied"}),
    )
    with pytest.raises(InputError, match="PROFILE_DEPENDENCIES_UNRESOLVED"):
        frozen(bundle)


def test_profile_schema_matches_export() -> None:
    path = EXAMPLE.parents[1] / "schemas" / "fixed-profile-v1.schema.json"
    assert path.read_bytes() == json_bytes(FixedComparisonProfile.model_json_schema())


def test_profile_symlink_rejected(bundle: Path, tmp_path: Path) -> None:
    profile = frozen(bundle)
    target = tmp_path / "target.json"
    write_profile(profile, target, input_root=bundle)
    path = tmp_path / "profile.json"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation requires OS privilege; Linux CI covers this.")
    with pytest.raises(InputError, match="LINK_REFERENCE_REJECTED"):
        read_profile(path, expected_sha256=profile_digest(profile))


def test_additional_fixed_node_and_display_only_change(bundle: Path) -> None:
    identity = load_case(bundle).documents["workflow/before.json"]["nodes"][0]["id"]
    profile = freeze_profile(
        load_case(bundle), profile_id="p", revision="1", additional_fixed_node_ids=(identity,)
    )
    edit_document(bundle, "workflow/after.json", lambda w: w["nodes"][0].update(position=[5, 6]))
    validate_profile(load_case(bundle), profile)
    edit_document(bundle, "workflow/after.json", lambda w: w["nodes"][0].update(disabled=True))
    with pytest.raises(InputError, match="PROFILE_FIXED_NODE_CHANGED"):
        validate_profile(load_case(bundle), profile)


def test_forged_profile_wrong_pin_and_existing_output_rejected(
    bundle: Path, tmp_path: Path
) -> None:
    profile = frozen(bundle)
    path = tmp_path / "profile.json"
    write_profile(profile, path, input_root=bundle)
    with pytest.raises(InputError, match="OUTPUT_ALREADY_EXISTS"):
        write_profile(profile, path, input_root=bundle)
    with pytest.raises(InputError, match="PROFILE_IDENTITY_MISMATCH"):
        read_profile(path, expected_sha256="0" * 64)
    forged = profile.model_copy(update={"revision": "forged"})
    path.write_bytes(json_bytes(forged.model_dump(mode="json")))
    with pytest.raises(InputError, match="PROFILE_IDENTITY_MISMATCH"):
        read_profile(path, expected_sha256=profile_digest(profile))


def test_cli_refuses_drift_before_writing_handoff(bundle: Path, tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    cli = CliRunner()
    result = cli.invoke(
        app,
        [
            "freeze-profile",
            str(bundle),
            "--out",
            str(path),
            "--profile-id",
            "example",
            "--revision",
            "1.0.0",
        ],
    )
    assert result.exit_code == 0, result.output
    pin = json.loads(result.output)["profile_sha256"]
    edit_document(bundle, "environment.json", lambda v: v.update(target_version="9.9.9"))
    out = tmp_path / "handoff"
    result = cli.invoke(
        app,
        [
            "profile-snapshot",
            str(bundle),
            "--profile",
            str(path),
            "--profile-sha256",
            pin,
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2
    assert "PROFILE_FIXED_INPUT_CHANGED" in result.output
    assert not out.exists()
