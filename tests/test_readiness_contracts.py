import json
from pathlib import Path

import pytest
from conftest import edit_document, refresh_manifest
from pydantic import ValidationError

from flowdelta.contracts import ArtifactRef, InputContract
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case
from flowdelta.migration import migrate_case_03
from flowdelta.pipeline_contracts import InputField, InputValueSchema
from flowdelta.pipeline_io import read_handoff, validate_input_fields


def add_dependencies(bundle: Path, dependency_document: dict, files: dict) -> None:
    case = json.loads((bundle / "case.json").read_bytes())
    case["dependency_manifest_ref"] = "dependencies.json"
    (bundle / "case.json").write_bytes(json_bytes(case))
    files = {"dependencies.json": {"schema_version": "0.4.0", **dependency_document}, **files}
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    manifest["artifacts"] = [
        ref for ref in manifest["artifacts"] if not ref["path"].startswith("dependencies")
    ]
    for index, (relative, content) in enumerate(files.items()):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json_bytes(content)
        path.write_bytes(data)
        manifest["artifacts"].append(
            ArtifactRef(
                artifact_id=f"dependency-{index}",
                path=relative,
                sha256=digest(data),
                size_bytes=len(data),
                kind="source",
            ).model_dump(mode="json")
        )
    (bundle / "manifest.json").write_bytes(json_bytes(manifest))
    refresh_manifest(bundle)


def runtime_dependencies() -> dict:
    return {
        "dependencies": [
            {
                "dependency_id": "runtime",
                "kind": "RUNTIME_CONFIG",
                "version": "2.42.6",
                "path": "deps/runtime.json",
                "requires": [],
            }
        ],
        "before_roots": ["runtime"],
        "after_roots": ["runtime"],
    }


def test_dependency_hashing_and_round_trip(bundle: Path, tmp_path: Path) -> None:
    add_dependencies(bundle, runtime_dependencies(), {"deps/runtime.json": {"image": "sha256:one"}})
    first = build_handoff(load_case(bundle))
    write_handoff(load_case(bundle), first, tmp_path / "output")
    assert read_handoff(tmp_path / "output") == first
    assert all(s.semantic_sha256 for s in first.snapshots.snapshots)
    assert all(s.dependency_status == "DECLARED_RESOLVED" for s in first.snapshots.snapshots)
    edit_document(bundle, "deps/runtime.json", lambda value: value.update(image="sha256:two"))
    second = build_handoff(load_case(bundle))
    for before, after in zip(first.snapshots.snapshots, second.snapshots.snapshots, strict=True):
        assert before.raw_sha256 == after.raw_sha256
        assert before.snapshot_id != after.snapshot_id
        assert before.semantic_sha256 != after.semantic_sha256


def test_display_only_change_preserves_config_hash_but_not_raw_hash(bundle: Path) -> None:
    add_dependencies(bundle, runtime_dependencies(), {"deps/runtime.json": {"image": "sha256:one"}})
    first = build_handoff(load_case(bundle)).snapshots.snapshots[0]
    edit_document(bundle, "workflow/before.json", lambda v: v["nodes"][0].update(position=[7, 8]))
    second = build_handoff(load_case(bundle)).snapshots.snapshots[0]
    assert first.raw_sha256 != second.raw_sha256
    assert first.semantic_sha256 == second.semantic_sha256
    edit_document(bundle, "workflow/before.json", lambda v: v["nodes"][0].update(futureFlag=True))
    third = build_handoff(load_case(bundle)).snapshots.snapshots[0]
    assert second.semantic_sha256 != third.semantic_sha256


@pytest.mark.parametrize(
    "failure,code",
    [
        ("cycle", "DEPENDENCY_CYCLE"),
        ("missing", "DEPENDENCY_NOT_DECLARED"),
        ("unreachable", "DEPENDENCY_UNREACHABLE"),
    ],
)
def test_dependency_graph_rejects_incomplete_graph(bundle: Path, failure: str, code: str) -> None:
    manifest = runtime_dependencies()
    if failure == "cycle":
        manifest["dependencies"][0]["requires"] = ["runtime"]
    elif failure == "missing":
        manifest["dependencies"][0]["requires"] = ["absent"]
    else:
        manifest["before_roots"] = manifest["after_roots"] = []
    add_dependencies(bundle, manifest, {"deps/runtime.json": {"image": "sha256:one"}})
    with pytest.raises(InputError, match=code):
        load_case(bundle)


def test_unresolved_subworkflow_never_gets_complete_hash(bundle: Path) -> None:
    add_dependencies(bundle, runtime_dependencies(), {"deps/runtime.json": {"image": "sha256:one"}})

    def change(value):
        node = value["nodes"][1]
        node.update(
            type="n8n-nodes-base.executeWorkflow",
            typeVersion=1.4,
            parameters={"workflowId": "absent", "source": "database"},
        )

    edit_document(bundle, "workflow/before.json", change)
    result = build_handoff(load_case(bundle)).snapshots.snapshots[0]
    assert result.semantic_sha256 is None
    assert result.dependency_limits == ["FIXED_SUBWORKFLOW_NOT_SUPPLIED"]


def test_nested_inputs_and_types_are_enforced() -> None:
    fields = [
        InputField(
            name="tickets",
            required=True,
            value_type="array",
            max_items=2,
            items=InputValueSchema(
                value_type="object",
                properties=[
                    InputField(
                        name="ticket_id", value_type="string", required=True, pattern="T-[0-9]+"
                    ),
                    InputField(name="urgent", value_type="boolean", required=True),
                    InputField(name="priority", value_type="integer", required=True),
                ],
            ),
        )
    ]
    valid = {"ticket_id": "T-1", "urgent": True, "priority": 3}
    validate_input_fields({"tickets": [valid, valid]}, fields)
    for value in [
        {"tickets": [valid] * 3},
        {"tickets": [{**valid, "priority": True}]},
        {"tickets": [{**valid, "urgent": "true"}]},
        {"tickets": [{**valid, "extra": 2}]},
        {"tickets": [{"ticket_id": "T-1"}]},
    ]:
        with pytest.raises(InputError):
            validate_input_fields(value, fields)


def test_explicit_legacy_migration_keeps_workflow_bytes(bundle: Path, tmp_path: Path) -> None:
    # The legacy bundle has no dependency manifest. Change only the contract version metadata.
    for relative in [
        "case.json",
        "task.json",
        "policy.json",
        "environment.json",
        "state/initial.json",
        "provenance.json",
    ]:
        value = json.loads((bundle / relative).read_bytes())
        value.pop("dependency_manifest_ref", None)
        value["schema_version"] = "0.3.0"
        for items in [value.get("rules", []), value.get("obligations", [])]:
            for item in items:
                item["schema_version"] = "0.3.0"
        (bundle / relative).write_bytes(json_bytes(value))
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    manifest["artifacts"] = [
        ref for ref in manifest["artifacts"] if not ref["path"].startswith("dependencies")
    ]
    manifest["schema_version"] = "0.3.0"
    for ref in manifest["artifacts"]:
        ref["schema_version"] = "0.3.0"
    (bundle / "manifest.json").write_bytes(json_bytes(manifest))
    refresh_manifest(bundle)
    source_hashes = {p: digest(p.read_bytes()) for p in bundle.rglob("*.json")}
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        load_case(bundle)
    migrated = migrate_case_03(bundle, tmp_path / "converted")
    assert load_case(migrated).case.schema_version == "0.4.0"
    assert all(digest(p.read_bytes()) == value for p, value in source_hashes.items())
    for relative in ["workflow/before.json", "workflow/after.json"]:
        assert (migrated / relative).read_bytes() == (bundle / relative).read_bytes()
    with pytest.raises(InputError, match="OUTPUT_ALREADY_EXISTS"):
        migrate_case_03(bundle, migrated)


def test_mixed_schema_versions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        InputContract.model_validate({"schema_version": "0.3.0"})
