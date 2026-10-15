import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.ingestion import InputError, json_bytes
from flowdelta.module_outputs import read_module_outputs
from flowdelta.pipeline_bundle import write_module_bundle
from flowdelta.pipeline_io import read_manifest, read_pipeline_examples

REFERENCE = Path(__file__).resolve().parents[1] / "examples/contracts/order-lookup"


def selected() -> dict[str, Path]:
    manifest, _ = read_manifest(REFERENCE)
    return {
        ref.path: REFERENCE / ref.path
        for ref in manifest.artifacts
        if not ref.path.startswith("input/")
    }


def test_preserves_reference_bytes_ids_and_authorship(tmp_path):
    output = tmp_path / "bundle"
    manifest = write_module_bundle(REFERENCE / "input", selected(), output)
    assert read_manifest(output)[0] == manifest
    read_pipeline_examples(output)
    for path in (REFERENCE / "input").rglob("*.json"):
        assert (
            output / "input" / path.relative_to(REFERENCE / "input")
        ).read_bytes() == path.read_bytes()
    for name, path in selected().items():
        assert (output / name).read_bytes() == path.read_bytes()
    with pytest.raises(InputError, match="^MODULE_OUTPUT_REQUIRED$"):
        read_module_outputs(output)


@pytest.mark.parametrize("name", ["../escape.json", "C:/escape.json", "runs\\escape.json"])
def test_bad_destination_paths_never_publish(tmp_path, name):
    with pytest.raises(InputError, match="^ARTIFACT_PATH_INVALID$"):
        write_module_bundle(
            REFERENCE / "input", {name: REFERENCE / "analysis.json"}, tmp_path / "out"
        )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["manifest.json", "input/extra.json", "INPUT/file.json"])
def test_reserved_input_and_manifest_paths_are_rejected(tmp_path, name):
    with pytest.raises(InputError, match="^RESERVED_ARTIFACT_PATH$"):
        write_module_bundle(
            REFERENCE / "input", {name: REFERENCE / "analysis.json"}, tmp_path / "out"
        )


def test_path_case_and_parent_collisions_are_rejected(tmp_path):
    for paths in [("a.json", "A.json"), ("a.json", "a.json/child.json")]:
        with pytest.raises(InputError, match="^ARTIFACT_PATH_COLLISION$"):
            write_module_bundle(
                REFERENCE / "input",
                dict.fromkeys(paths, REFERENCE / "analysis.json"),
                tmp_path / "out",
            )


def test_existing_output_is_unchanged(tmp_path):
    (tmp_path / "keep").write_bytes(b"unchanged")
    with pytest.raises(InputError, match="^OUTPUT_ALREADY_EXISTS$"):
        write_module_bundle(REFERENCE / "input", selected(), tmp_path)
    assert list(tmp_path.iterdir()) == [tmp_path / "keep"]
    assert (tmp_path / "keep").read_bytes() == b"unchanged"


def test_missing_source_and_reference_are_rejected(tmp_path):
    with pytest.raises(InputError, match="^ARTIFACT_SOURCE_MISSING$"):
        write_module_bundle(
            REFERENCE / "input", {"missing.json": tmp_path / "missing"}, tmp_path / "out"
        )
    doc = json.loads((REFERENCE / "analysis.json").read_bytes())
    doc["task_ref"]["path"] = "absent.json"
    path = tmp_path / "analysis.json"
    path.write_bytes(json_bytes(doc))
    with pytest.raises(InputError, match="^ARTIFACT_SOURCE_MISSING$"):
        write_module_bundle(REFERENCE / "input", {"analysis.json": path}, tmp_path / "out")


def test_tampered_producer_reference_rejected_before_output(tmp_path):
    doc = json.loads((REFERENCE / "analysis.json").read_bytes())
    doc["task_ref"]["sha256"] = "0" * 64
    path = tmp_path / "analysis.json"
    path.write_bytes(json_bytes(doc))
    with pytest.raises(InputError, match="^ARTIFACT_INTEGRITY_MISMATCH$"):
        write_module_bundle(REFERENCE / "input", {"analysis.json": path}, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unselected_and_private_files_are_not_collected(tmp_path):
    secret = tmp_path / "private" / "credential.json"
    secret.parent.mkdir()
    secret.write_bytes(b'{"secret":"local-test-only"}')
    output = tmp_path / "out"
    write_module_bundle(REFERENCE / "input", {"analysis.json": REFERENCE / "analysis.json"}, output)
    assert not (output / "private").exists()
    with pytest.raises(InputError, match="^PRIVATE_ARTIFACT_FORBIDDEN$"):
        write_module_bundle(REFERENCE / "input", {"credential.json": secret}, tmp_path / "other")


def test_partial_publish_has_no_manifest(monkeypatch, tmp_path):
    rename = Path.rename
    output = tmp_path / "out"

    def fail_manifest(self, target):
        if self.name == "manifest.json":
            assert (output / "input").is_dir()
            raise OSError("simulated publication failure")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_manifest)
    with pytest.raises(InputError, match="^ARTIFACT_WRITE_ERROR$"):
        write_module_bundle(REFERENCE / "input", selected(), output)
    assert output.is_dir() and not (output / "manifest.json").exists()
    assert not list(tmp_path.glob(".flowdelta-bundle-*"))


def test_cli_explicit_mapping(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "bundle",
            str(REFERENCE / "input"),
            "--artifact",
            f"analysis.json={REFERENCE / 'analysis.json'}",
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "BUNDLE_WRITTEN"
    assert (tmp_path / "out/analysis.json").read_bytes() == (
        REFERENCE / "analysis.json"
    ).read_bytes()
