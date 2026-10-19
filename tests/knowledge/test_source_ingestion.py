import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError, digest
from flowdelta.knowledge import source_ingestion as source_ingestion_module
from flowdelta.knowledge.database import engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.source_ingestion import (
    MAX_PAGE_SIZE,
    Cursor,
    IngestionReport,
    RawSourceRecord,
    SourcePage,
    ingest_source,
)


@dataclass
class FixtureSource:
    source: str
    pages: list[SourcePage | Exception]
    calls: list[tuple[Cursor, int]]

    def fetch_page(self, cursor: Cursor, page_size: int) -> SourcePage:
        self.calls.append((cursor, page_size))
        response = self.pages.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def normalize(self, document):
        return {"identifier": document["id"], "summary": document.get("summary", "")}


@pytest.fixture
def database(tmp_path: Path):
    app_url = os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL")
    if not app_url:
        pytest.skip("requires an explicitly configured disposable PostgreSQL database")
    settings = KnowledgeSettings(
        database_url=app_url,
        migration_url=os.environ.get("FLOWDELTA_KB_TEST_MIGRATION_URL", app_url),
        raw_root=tmp_path,
    )
    migrate(settings)
    engine = engine_for(settings)
    migration_engine = engine_for(settings, migration=True)
    source = f"test-{uuid4().hex}"
    try:
        yield engine, source, tmp_path
    finally:
        with migration_engine.begin() as connection:
            for table in ("sync_failure", "source_record", "source_revision", "sync_state"):
                connection.execute(
                    text(f"DELETE FROM knowledge.{table} WHERE source=:source"),
                    {"source": source},
                )
        engine.dispose()
        migration_engine.dispose()


def test_pages_resume_and_preserve_raw_revisions(database):
    engine, source, raw_root = database
    raw = b'{"id":"VULN-SAMPLE-1", "summary":"source form"}'
    malformed = b'{"id":'
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (RawSourceRecord("VULN-SAMPLE-1", raw, datetime(2026, 1, 2, tzinfo=UTC)),),
                {"page": 1},
                False,
            ),
            SourcePage((RawSourceRecord("VULN-BAD-1", malformed),), {"page": 1}, True),
        ],
        [],
    )

    first = ingest_source(engine, raw_root, adapter, page_size=5, max_pages=1)
    second = ingest_source(engine, raw_root, adapter, page_size=5, max_pages=1)

    assert first == IngestionReport(source, "PENDING", 1, 1, 1, 0)
    assert second == IngestionReport(source, "COMPLETE", 1, 1, 0, 1)
    assert adapter.calls == [({}, 5), ({"page": 1}, 5)]
    with engine.connect() as connection:
        state = (
            connection.execute(
                text(
                    "SELECT cursor, status, records_seen, error_code FROM knowledge.sync_state "
                    "WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        record = (
            connection.execute(
                text("SELECT sha256, document FROM knowledge.source_record WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
        revision = (
            connection.execute(
                text(
                    "SELECT raw_path, sha256, size_bytes FROM knowledge.source_revision "
                    "WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        failure = (
            connection.execute(
                text("SELECT error_code, sha256 FROM knowledge.sync_failure WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )

    assert state == {
        "cursor": {"page": 1},
        "status": "COMPLETE",
        "records_seen": 2,
        "error_code": None,
    }
    assert record["sha256"] == digest(raw)
    assert record["document"] == {"identifier": "VULN-SAMPLE-1", "summary": "source form"}
    assert (raw_root / revision["raw_path"]).read_bytes() == raw
    assert revision["sha256"] == digest(raw)
    assert revision["size_bytes"] == len(raw)
    assert failure == {"error_code": "KNOWLEDGE_SOURCE_RECORD_INVALID", "sha256": digest(malformed)}


def test_duplicate_revision_is_idempotent_across_pages(database):
    engine, source, raw_root = database
    raw = b'{"id":"VULN-SAMPLE-2", "summary":"same source record"}'
    repeated = RawSourceRecord("VULN-SAMPLE-2", raw)
    adapter = FixtureSource(
        source,
        [
            SourcePage((repeated,), {"page": 1}, False),
            SourcePage((repeated,), {"page": 2}, True),
        ],
        [],
    )

    ingest_source(engine, raw_root, adapter, max_pages=1)
    report = ingest_source(engine, raw_root, adapter, max_pages=1)

    assert report.records_saved == 1
    with engine.connect() as connection:
        revisions = connection.execute(
            text("SELECT count(*) FROM knowledge.source_revision WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        records = connection.execute(
            text("SELECT count(*) FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert revisions == 1
    assert records == 1


def test_invalid_record_id_is_quarantined_without_stopping_page(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord("", b'{"id":"VULN-INVALID-ID"}'),
                    RawSourceRecord("VULN-SAMPLE-4", b'{"id":"VULN-SAMPLE-4"}'),
                ),
                {"page": 1},
                True,
            )
        ],
        [],
    )

    report = ingest_source(engine, raw_root, adapter)

    assert report.records_saved == 1
    assert report.records_failed == 1
    with engine.connect() as connection:
        failure = (
            connection.execute(
                text(
                    "SELECT external_id, error_code FROM knowledge.sync_failure "
                    "WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert failure["external_id"].startswith("invalid-")
    assert failure["error_code"] == "KNOWLEDGE_SOURCE_RECORD_ID_INVALID"


def test_page_records_and_cursor_roll_back_together(database, monkeypatch):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord("VULN-SAMPLE-5", b'{"id":"VULN-SAMPLE-5"}'),
                    RawSourceRecord("VULN-SAMPLE-6", b'{"id":"VULN-SAMPLE-6"}'),
                ),
                {"page": 1},
                False,
            )
        ],
        [],
    )
    original_save_record = source_ingestion_module.save_record
    calls = 0

    def fail_after_second_record(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_save_record(*args, **kwargs)
        if calls == 2:
            raise SQLAlchemyError("sensitive database detail")
        return result

    monkeypatch.setattr(source_ingestion_module, "save_record", fail_after_second_record)

    with pytest.raises(InputError, match="KNOWLEDGE_DATABASE_UNAVAILABLE") as error:
        ingest_source(engine, raw_root, adapter)

    assert "sensitive database detail" not in str(error.value)
    with engine.connect() as connection:
        state = (
            connection.execute(
                text(
                    "SELECT cursor, status, error_code FROM knowledge.sync_state "
                    "WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        revisions = connection.execute(
            text("SELECT count(*) FROM knowledge.source_revision WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        records = connection.execute(
            text("SELECT count(*) FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()

    assert state == {
        "cursor": {},
        "status": "FAILED",
        "error_code": "KNOWLEDGE_DATABASE_UNAVAILABLE",
    }
    assert revisions == 0
    assert records == 0


def test_fetch_failure_keeps_last_committed_cursor_and_hides_details(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage((), {"offset": 20}, False),
            RuntimeError("provider response included sensitive input"),
        ],
        [],
    )
    ingest_source(engine, raw_root, adapter, max_pages=1)

    with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_FETCH_FAILED") as error:
        ingest_source(engine, raw_root, adapter)

    assert "sensitive input" not in str(error.value)
    with engine.connect() as connection:
        state = (
            connection.execute(
                text(
                    "SELECT cursor, status, error_code "
                    "FROM knowledge.sync_state WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {
        "cursor": {"offset": 20},
        "status": "FAILED",
        "error_code": "KNOWLEDGE_SOURCE_FETCH_FAILED",
    }


def test_stalled_cursor_rejects_page_without_persisting_records(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (RawSourceRecord("VULN-SAMPLE-3", b'{"id":"VULN-SAMPLE-3"}'),),
                {},
                False,
            )
        ],
        [],
    )

    with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_CURSOR_STALLED"):
        ingest_source(engine, raw_root, adapter)

    with engine.connect() as connection:
        state = (
            connection.execute(
                text(
                    "SELECT cursor, status, error_code "
                    "FROM knowledge.sync_state WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        records = connection.execute(
            text("SELECT count(*) FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert state == {
        "cursor": {},
        "status": "FAILED",
        "error_code": "KNOWLEDGE_SOURCE_CURSOR_STALLED",
    }
    assert records == 0


def test_ingestion_limits_are_checked_before_database_access(database):
    engine, source, _ = database
    adapter = FixtureSource(source, [], [])
    with pytest.raises(InputError, match="KNOWLEDGE_INGESTION_LIMIT_INVALID"):
        ingest_source(engine, Path("."), adapter, page_size=MAX_PAGE_SIZE + 1)
