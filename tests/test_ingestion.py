import json
from pathlib import Path

import pytest
from conftest import edit_document, refresh_manifest

from flowdelta import ingestion
from flowdelta.ingestion import InputError, json_bytes, load_case, parse_json


def test_normal_bundle_preserves_fixed_context_and_obligations(bundle: Path) -> None:
    loaded = load_case(bundle)
    assert loaded.case.changed_axes == ["workflow"]
    assert len(loaded.case.obligations) == 2
    assert loaded.case.baseline_security_status == "UNKNOWN"
    assert loaded.policy.interpretation == "DECLARED_ONLY"
    assert loaded.provenance.source_kind == "TEAM_SYNTHETIC"
    assert loaded.environment.runtime_status == "NOT_VALIDATED"
    assert len(loaded.raw) == 10


def test_modified_source_cannot_pass_original_manifest(bundle: Path) -> None:
    with (bundle / "task.json").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        load_case(bundle)


@pytest.mark.parametrize(
    "reference",
    [
        "../task.json",
        "/task.json",
        "a/../task.json",
        "a\\b.json",
        "https://example.test/a",
        "CON.json",
    ],
)
def test_nonportable_or_external_references_are_rejected(bundle: Path, reference: str) -> None:
    edit_document(bundle, "case.json", lambda value: value.update(task_ref=reference))
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        load_case(bundle)


@pytest.mark.parametrize(
    "update",
    [
        lambda v: v.update(schema_version="0.2.0"),
        lambda v: v.pop("schema_version"),
        lambda v: v.update(unknown_field=True),
        lambda v: v.update(case_id=42),
        lambda v: v.update(changed_axes=["policy"]),
        lambda v: v.update(changed_axes=["workflow", "workflow"]),
        lambda v: v.update(extensions={"unqualified": True}),
        lambda v: v.update(after_ref=v["before_ref"]),
        lambda v: v["obligations"].append(v["obligations"][0]),
    ],
)
def test_contract_mismatch_is_explicit(bundle: Path, update) -> None:
    edit_document(bundle, "case.json", update)
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        load_case(bundle)


def test_missing_reference_and_extra_manifest_entry(bundle: Path) -> None:
    edit_document(bundle, "case.json", lambda value: value.update(task_ref="missing.json"))
    with pytest.raises(InputError, match="MANIFEST_REFERENCE_MISMATCH"):
        load_case(bundle)


def test_missing_file_is_not_ignored(bundle: Path) -> None:
    (bundle / "task.json").unlink()
    with pytest.raises(InputError, match="MISSING_ARTIFACT"):
        load_case(bundle)


def test_casefold_manifest_duplicates_are_rejected(bundle: Path) -> None:
    path = bundle / "manifest.json"
    value = json.loads(path.read_bytes())
    value["artifacts"][1]["path"] = value["artifacts"][0]["path"].upper()
    path.write_bytes(json_bytes(value))
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        load_case(bundle)


def test_provenance_cannot_silently_point_elsewhere(bundle: Path) -> None:
    edit_document(bundle, "task.json", lambda value: value.update(provenance_ref="task.json"))
    with pytest.raises(InputError, match="PROVENANCE_REFERENCE_MISMATCH"):
        load_case(bundle)


@pytest.mark.parametrize(
    "data, code",
    [
        (b'{"a": 1, "a": 2}', "DUPLICATE_JSON_KEY"),
        (b'{"a": NaN}', "NON_FINITE_JSON_NUMBER"),
        (b'{"a": 1e999}', "NON_FINITE_JSON_NUMBER"),
        (b"[]", "JSON_OBJECT_REQUIRED"),
        (b'{"a": "\\ud800"}', "INVALID_JSON"),
        (b'{"\\ud800": 1}', "INVALID_JSON"),
        (b'{"a": ', "INVALID_JSON"),
        (b'{"a":' + b"[" * 70 + b"0" + b"]" * 70 + b"}", "JSON_DEPTH_LIMIT"),
    ],
)
def test_strict_json(data: bytes, code: str) -> None:
    with pytest.raises(InputError, match=code):
        parse_json(data)


def test_file_budget(bundle: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingestion, "MAX_FILE_BYTES", 32)
    with pytest.raises(InputError, match="FILE_SIZE_LIMIT"):
        load_case(bundle)


def test_bundle_budget(bundle: Path, monkeypatch) -> None:
    monkeypatch.setattr(ingestion, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(InputError, match="BUNDLE_SIZE_LIMIT"):
        load_case(bundle)


def test_links_are_not_followed(bundle: Path) -> None:
    path = bundle / "task.json"
    backup = bundle.parent / "outside.json"
    path.rename(backup)
    try:
        path.symlink_to(backup)
    except OSError:
        pytest.skip("OS does not grant symlink creation; CI Linux covers this case")
    with pytest.raises(InputError, match="LINK_REFERENCE_REJECTED"):
        load_case(bundle)


def test_manifest_does_not_make_invalid_json_valid(bundle: Path) -> None:
    (bundle / "task.json").write_bytes(b'{"a": 1, "a": 2}')
    refresh_manifest(bundle)
    with pytest.raises(InputError, match="DUPLICATE_JSON_KEY"):
        load_case(bundle)
