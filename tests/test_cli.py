import json
from pathlib import Path

import pytest
from conftest import edit_document
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.contracts import Handoff
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case

runner = CliRunner()


def test_cli_round_trip_handoff(bundle: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["inspect-case", str(bundle)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["execution_status"] == "NOT_RUN"
    output = tmp_path / "handoff"
    created = runner.invoke(app, ["snapshot", str(bundle), "--out", str(output)])
    assert created.exit_code == 0, created.output
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert len(manifest["artifacts"]) == 13
    for ref in manifest["artifacts"]:
        data = (output / ref["path"]).read_bytes()
        assert ref["sha256"] == digest(data)
        assert ref["size_bytes"] == len(data)
        if ref["kind"] == "source":
            assert data == (bundle / ref["path"]).read_bytes()
    before = json.loads((output / "before.normalized.json").read_bytes())
    assert before["semantic_support"] == "UNKNOWN"
    snapshots = json.loads((output / "snapshots.json").read_bytes())["snapshots"]
    assert len(snapshots) == 2
    assert all(item["semantic_sha256"] is None for item in snapshots)
    assert not list(tmp_path.glob(".flowdelta-*"))


def test_existing_output_is_never_overwritten(bundle: Path, tmp_path: Path) -> None:
    output = tmp_path / "handoff"
    output.mkdir()
    witness = output / "user.txt"
    witness.write_text("keep", encoding="utf-8")
    result = runner.invoke(app, ["snapshot", str(bundle), "--out", str(output)])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "OUTPUT_ALREADY_EXISTS"
    assert witness.read_text(encoding="utf-8") == "keep"
    assert list(output.iterdir()) == [witness]


def test_output_cannot_be_written_into_input(bundle: Path) -> None:
    result = runner.invoke(app, ["snapshot", str(bundle), "--out", str(bundle / "output")])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "OUTPUT_OVERLAPS_INPUT"
    assert not (bundle / "output").exists()


def test_diagnostics_do_not_echo_input(bundle: Path) -> None:
    edit_document(
        bundle, "case.json", lambda value: value.update(private_note="demo-private-value")
    )
    result = runner.invoke(app, ["inspect-case", str(bundle)])
    assert result.exit_code == 2
    assert json.loads(result.stderr) == {"error": "CONTRACT_INVALID"}
    assert result.stdout == ""
    assert "demo-private-value" not in result.output


def test_generated_schema_matches_checked_in_contract() -> None:
    expected = Path(__file__).resolve().parents[1] / "schemas" / "input-0.3.0.schema.json"
    assert json_bytes(Handoff.model_json_schema()) == expected.read_bytes()
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == json.loads(expected.read_bytes())


def test_handoff_is_reproducible_across_output_paths(bundle: Path, tmp_path: Path) -> None:
    loaded = load_case(bundle)
    handoff = build_handoff(loaded)
    paths = [write_handoff(loaded, handoff, tmp_path / name) for name in ["one", "two"]]
    files = [path.relative_to(paths[0]) for path in paths[0].rglob("*") if path.is_file()]
    assert all((paths[0] / name).read_bytes() == (paths[1] / name).read_bytes() for name in files)


def test_write_failure_does_not_publish_manifest(bundle: Path, tmp_path: Path, monkeypatch) -> None:
    loaded = load_case(bundle)
    handoff = build_handoff(loaded)
    rename = Path.rename

    def fail_one(self, target):
        if self.name == "case.json":
            raise OSError("simulated disk failure")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_one)
    output = tmp_path / "handoff"
    with pytest.raises(InputError, match="ARTIFACT_WRITE_ERROR"):
        write_handoff(loaded, handoff, output)
    assert not (output / "manifest.json").exists()
    assert not list(tmp_path.glob(".flowdelta-*"))
