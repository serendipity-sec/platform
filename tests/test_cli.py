import json
from pathlib import Path
from types import SimpleNamespace

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
    assert len(manifest["artifacts"]) == 16
    for ref in manifest["artifacts"]:
        data = (output / ref["path"]).read_bytes()
        assert ref["sha256"] == digest(data)
        assert ref["size_bytes"] == len(data)
        if ref["kind"] == "source":
            assert data == (bundle / ref["path"]).read_bytes()
    before = json.loads((output / "before.normalized.json").read_bytes())
    assert before["semantic_support"] == "SUPPORTED"
    snapshots = json.loads((output / "snapshots.json").read_bytes())["snapshots"]
    assert len(snapshots) == 2
    assert all(item["semantic_sha256"] is not None for item in snapshots)
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
    expected = Path(__file__).resolve().parents[1] / "schemas" / "input-0.4.0.schema.json"
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


def test_shared_plan_uses_vector_planner_and_persists_outputs(tmp_path, monkeypatch):
    from flowdelta.knowledge import planner, repository
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    root = Path(__file__).resolve().parents[1] / "examples/contracts/order-lookup"
    out = tmp_path / "plan.json"
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    provider = object()
    disposed = []
    planned = []
    persisted = []

    def write_vector_plan(handoff, analysis, catalog, output, *, engine, provider, limit=50):
        planned.append((analysis.analysis_id, catalog.model_dump(mode="json"), engine, provider))
        output.write_bytes(b"exact-plan-output")
        return SimpleNamespace(plan_id="plan-shared-exact")

    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://app:secret@db/kb")
    monkeypatch.setattr(planner, "write_vector_plan", write_vector_plan)
    monkeypatch.setattr(
        planner, "write_plan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(
        planner, "load_cards", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr("flowdelta.knowledge.database.engine_for", lambda settings: engine)
    monkeypatch.setattr(EmbeddingSettings, "provider", lambda self: provider)
    monkeypatch.setattr(
        repository,
        "store_b_outputs",
        lambda actual_engine, path: persisted.append((actual_engine, path, path.read_bytes())),
    )

    result = runner.invoke(
        app,
        [
            "plan",
            str(root),
            "--catalog",
            str(root / "template-catalog.json"),
            "--out",
            str(out),
            "--shared-cards",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["database_status"] == "STORED"
    assert json.loads(result.stdout)["plan_id"] == "plan-shared-exact"
    assert planned and planned[0][0] == "analysis-order-lookup-example"
    assert planned[0][2:] == (engine, provider)
    assert persisted == [(engine, out, b"exact-plan-output")]
    assert disposed == [True]


def test_shared_plan_failure_does_not_fall_back_or_persist(tmp_path, monkeypatch):
    from flowdelta.knowledge import planner, repository
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    root = Path(__file__).resolve().parents[1] / "examples/contracts/order-lookup"
    out = tmp_path / "plan.json"
    disposed = []
    stores = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://app:secret@db/kb")
    monkeypatch.setattr(
        planner,
        "write_vector_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            InputError("CARD_CATALOG_PROFILE_NOT_INDEXED")
        ),
    )
    monkeypatch.setattr(
        planner, "write_plan", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr("flowdelta.knowledge.database.engine_for", lambda settings: engine)
    monkeypatch.setattr(EmbeddingSettings, "provider", lambda self: object())
    monkeypatch.setattr(repository, "store_b_outputs", lambda *args: stores.append(args))

    result = runner.invoke(
        app,
        [
            "plan",
            str(root),
            "--catalog",
            str(root / "template-catalog.json"),
            "--out",
            str(out),
            "--shared-cards",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "CARD_CATALOG_PROFILE_NOT_INDEXED"
    assert stores == []
    assert disposed == [True]


def test_plan_rejects_both_card_sources_before_database_access(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1] / "examples/contracts/order-lookup"
    engine_calls = []
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://app:secret@db/kb")
    monkeypatch.setattr(
        "flowdelta.knowledge.database.engine_for", lambda settings: engine_calls.append(settings)
    )

    result = runner.invoke(
        app,
        [
            "plan",
            str(root),
            "--catalog",
            str(root / "template-catalog.json"),
            "--cards",
            str(root / "cards"),
            "--out",
            str(tmp_path / "plan.json"),
            "--shared-cards",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "PLAN_CARD_SOURCE_CONFLICT"
    assert engine_calls == []
