import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.graph import analyze
from flowdelta.ingestion import InputError, json_bytes
from flowdelta.pipeline_contracts import AnalysisBundle
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    not DATABASE_URL, reason="set FLOWDELTA_KB_TEST_DATABASE_URL for PostgreSQL tests"
)


def analysis(case: str) -> AnalysisBundle:
    return analyze(read_handoff(ROOT / "examples/contracts" / case / "input"))


def unique(bundle: AnalysisBundle, label: str) -> AnalysisBundle:
    # Rows are immutable and the test database persists, so each test makes its own analysis
    # and case IDs instead of cleaning up; a damaged row then cannot affect another test.
    token = uuid.uuid4().hex[:12]
    return bundle.model_copy(
        update={"analysis_id": f"analysis-{label}-{token}", "case_id": f"case-{label}-{token}"}
    )


def test_graph_package_does_not_need_the_database_extra() -> None:
    code = "import sys, flowdelta.graph, flowdelta.cli; print('sqlalchemy' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert result.stdout.strip() == "False"


def test_cli_reports_missing_database_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLOWDELTA_KB_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["analysis-db", "get", "analysis-x", "--out", "unused.json"])
    assert result.exit_code == 2
    assert "KNOWLEDGE_DATABASE_URL_REQUIRED" in result.output


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    if not DATABASE_URL:
        pytest.skip("set FLOWDELTA_KB_TEST_DATABASE_URL for PostgreSQL tests")
    pytest.importorskip("sqlalchemy")
    from pydantic import SecretStr

    from flowdelta.graph.database import migrate_analyses
    from flowdelta.knowledge.database import engine_for
    from flowdelta.knowledge.settings import KnowledgeSettings

    migration = os.environ.get("FLOWDELTA_KB_TEST_MIGRATION_URL", DATABASE_URL)
    settings = KnowledgeSettings(
        database_url=SecretStr(DATABASE_URL), migration_url=SecretStr(migration)
    )
    migrate_analyses(settings)
    # Applying the same head again is a no-op.
    migrate_analyses(settings)
    app_engine = engine_for(settings)
    yield app_engine
    app_engine.dispose()


def repository(engine: object) -> object:
    from flowdelta.graph.database import DatabaseAnalysisRepository

    return DatabaseAnalysisRepository(engine)  # type: ignore[arg-type]


@needs_database
def test_round_trip_keeps_the_exact_analysis(engine: object) -> None:
    bundle = unique(analysis("support-triage"), "round")
    store = repository(engine)
    record = store.save(bundle)  # type: ignore[attr-defined]
    loaded = store.get(bundle.analysis_id)  # type: ignore[attr-defined]
    assert json_bytes(loaded.model_dump(mode="json")) == json_bytes(bundle.model_dump(mode="json"))
    assert record.sha256 == record.sha256.lower() and len(record.sha256) == 64
    assert record.extensions == bundle.extensions
    # Saving the same content again is a no-op.
    assert store.save(bundle) == record  # type: ignore[attr-defined]


@needs_database
def test_same_id_with_other_content_is_refused(engine: object) -> None:
    bundle = unique(analysis("order-lookup"), "conflict")
    store = repository(engine)
    store.save(bundle)  # type: ignore[attr-defined]
    changed = bundle.model_copy(update={"limitations": [*bundle.limitations, "다른 내용"]})
    with pytest.raises(InputError) as error:
        store.save(changed)  # type: ignore[attr-defined]
    assert error.value.code == "ANALYSIS_ID_CONFLICT"
    assert store.get(bundle.analysis_id) == bundle  # type: ignore[attr-defined]


@needs_database
def test_find_matches_the_exact_case_and_snapshots(engine: object) -> None:
    bundle = unique(analysis("order-lookup"), "find")
    store = repository(engine)
    store.save(bundle)  # type: ignore[attr-defined]
    found = store.find(  # type: ignore[attr-defined]
        bundle.case_id, bundle.before_snapshot_id, bundle.after_snapshot_id
    )
    assert bundle.analysis_id in [item.analysis_id for item in found]
    assert store.find(bundle.case_id, bundle.before_snapshot_id, "other") == []  # type: ignore[attr-defined]


@needs_database
def test_unknown_and_invalid_ids(engine: object) -> None:
    store = repository(engine)
    with pytest.raises(InputError) as missing:
        store.get("analysis-never-stored")  # type: ignore[attr-defined]
    assert missing.value.code == "ANALYSIS_NOT_FOUND"
    with pytest.raises(InputError) as invalid:
        store.get("../x")  # type: ignore[attr-defined]
    assert invalid.value.code == "ANALYSIS_ID_INVALID"


@needs_database
def test_rows_cannot_be_changed_or_deleted(engine: object) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    bundle = unique(analysis("order-lookup"), "immutable")
    repository(engine).save(bundle)  # type: ignore[attr-defined]
    for statement in (
        "UPDATE graph.analysis SET origin = 'CONTRACT_EXAMPLE' WHERE analysis_id = :id",
        "DELETE FROM graph.analysis WHERE analysis_id = :id",
    ):
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:  # type: ignore[attr-defined]
                connection.execute(text(statement), {"id": bundle.analysis_id})


@needs_database
def test_unreadable_analysis_is_not_stored(engine: object) -> None:
    bundle = unique(analysis("order-lookup"), "deep")
    nested: dict[str, object] = {"leaf": True}
    for _ in range(70):
        nested = {"next": nested}
    deep = bundle.model_copy(update={"extensions": {"test.deep": nested}})
    store = repository(engine)
    with pytest.raises(InputError) as error:
        store.save(deep)  # type: ignore[attr-defined]
    assert error.value.code == "JSON_DEPTH_LIMIT"
    with pytest.raises(InputError) as missing:
        store.get(bundle.analysis_id)  # type: ignore[attr-defined]
    assert missing.value.code == "ANALYSIS_NOT_FOUND"


@needs_database
def test_changed_payload_is_detected(engine: object) -> None:
    # Only the migration role can bypass the immutability trigger, as an operator would.
    from pydantic import SecretStr
    from sqlalchemy import text

    from flowdelta.knowledge.database import engine_for
    from flowdelta.knowledge.settings import KnowledgeSettings

    bundle = unique(analysis("order-lookup"), "tamper")
    store = repository(engine)
    store.save(bundle)  # type: ignore[attr-defined]
    migration = os.environ.get("FLOWDELTA_KB_TEST_MIGRATION_URL", DATABASE_URL or "")
    admin = engine_for(
        KnowledgeSettings(database_url=SecretStr(migration), migration_url=SecretStr(migration)),
        migration=True,
    )
    try:
        with admin.begin() as connection:
            connection.execute(text("ALTER TABLE graph.analysis DISABLE TRIGGER USER"))
            connection.execute(
                text(
                    "UPDATE graph.analysis SET payload = payload || '\\x20'::bytea "
                    "WHERE analysis_id = :id"
                ),
                {"id": bundle.analysis_id},
            )
            connection.execute(text("ALTER TABLE graph.analysis ENABLE TRIGGER USER"))
    finally:
        admin.dispose()
    with pytest.raises(InputError) as error:
        store.get(bundle.analysis_id)  # type: ignore[attr-defined]
    assert error.value.code == "ANALYSIS_INTEGRITY_MISMATCH"


@needs_database
def test_cli_store_and_get(engine: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = unique(analysis("support-triage"), "cli")
    source = tmp_path / "analysis.json"
    source.write_bytes(json_bytes(bundle.model_dump(mode="json")))
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", DATABASE_URL or "")
    runner = CliRunner()
    stored = runner.invoke(app, ["analysis-db", "store", str(source)])
    assert stored.exit_code == 0, stored.output
    assert bundle.analysis_id in stored.output
    out = tmp_path / "copy.json"
    fetched = runner.invoke(app, ["analysis-db", "get", bundle.analysis_id, "--out", str(out)])
    assert fetched.exit_code == 0, fetched.output
    assert out.read_bytes() == source.read_bytes()
    again = runner.invoke(app, ["analysis-db", "get", bundle.analysis_id, "--out", str(out)])
    assert again.exit_code == 2
    assert "OUTPUT_EXISTS" in again.output
