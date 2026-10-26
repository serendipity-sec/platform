import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError, digest
from flowdelta.knowledge import source_ingestion as source_ingestion_module
from flowdelta.knowledge.database import engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.source_ingestion import (
    MAX_PAGE_SIZE,
    MAX_PAGES_PER_RUN,
    Cursor,
    IngestionReport,
    RawSourceRecord,
    RetryReport,
    SourcePage,
    ingest_source,
    retry_quarantined_records,
)
from flowdelta.knowledge.storage import cleanup_orphaned_raw


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


@pytest.mark.parametrize(
    ("page_size", "max_pages"),
    [(MAX_PAGE_SIZE + 1, 1), (1, MAX_PAGES_PER_RUN + 1)],
)
def test_ingestion_limits_are_checked_before_database_access(page_size, max_pages):
    adapter = FixtureSource("test-limit", [], [])
    with pytest.raises(InputError, match="KNOWLEDGE_INGESTION_LIMIT_INVALID"):
        ingest_source(
            cast(Engine, object()),
            Path("."),
            adapter,
            page_size=page_size,
            max_pages=max_pages,
        )


@pytest.mark.parametrize("external_id", ["VULN\x00SAMPLE", "\ud800"])
def test_nul_or_unpaired_surrogate_id_is_quarantined(database, external_id):
    engine, source, raw_root = database
    raw = b'{"id":"VULN-SAMPLE-7"}'
    valid_raw = b'{"id":"VULN-SAMPLE-7","summary":"valid"}'
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord(external_id, raw),
                    RawSourceRecord("VULN-SAMPLE-7", valid_raw),
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
        failures = (
            connection.execute(
                text(
                    "SELECT external_id, error_code, raw_path, raw_external_id "
                    "FROM knowledge.sync_failure WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .all()
        )
    assert len(failures) == 1
    failure = failures[0]
    assert failure["external_id"].startswith("invalid-")
    assert failure["error_code"] == "KNOWLEDGE_SOURCE_RECORD_ID_INVALID"
    assert failure["raw_external_id"] == external_id.encode("utf-8", "surrogatepass")
    assert failure["raw_path"] is not None
    assert (raw_root / failure["raw_path"]).read_bytes() == raw


def test_jsonb_incompatible_record_is_quarantined_and_page_continues(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord(
                        "VULN-NUL-DOC",
                        b'{"id":"VULN-NUL-DOC","summary":"abc\\u0000x"}',
                    ),
                    RawSourceRecord("VULN-GOOD-DOC", b'{"id":"VULN-GOOD-DOC","summary":"good"}'),
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
        state = (
            connection.execute(
                text("SELECT status, cursor FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
        failure = (
            connection.execute(
                text(
                    "SELECT error_code, raw_path FROM knowledge.sync_failure WHERE source=:source"
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        saved_ids = (
            connection.execute(
                text("SELECT external_id FROM knowledge.source_record WHERE source=:source"),
                {"source": source},
            )
            .scalars()
            .all()
        )
    assert state == {"status": "COMPLETE", "cursor": {"page": 1}}
    assert failure["error_code"] == "KNOWLEDGE_SOURCE_RECORD_INVALID"
    assert failure["raw_path"] is not None
    assert saved_ids == ["VULN-GOOD-DOC"]


@dataclass
class ToggleSource(FixtureSource):
    fail_normalization: bool = True

    def normalize(self, document):
        if self.fail_normalization:
            raise KeyError("adapter mapping is temporarily incomplete")
        return super().normalize(document)


def test_quarantined_record_can_be_reprocessed_from_retained_raw(database):
    engine, source, raw_root = database
    adapter = ToggleSource(
        source,
        [
            SourcePage(
                (RawSourceRecord("VULN-RETRY", b'{"id":"VULN-RETRY"}'),),
                {"page": 1},
                True,
            )
        ],
        [],
    )
    first = ingest_source(engine, raw_root, adapter)
    with engine.connect() as connection:
        raw_path = connection.execute(
            text("SELECT raw_path FROM knowledge.sync_failure WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    retained_file = raw_root / raw_path
    os.utime(retained_file, (0, 0))
    assert cleanup_orphaned_raw(engine, raw_root, older_than_seconds=0) == 0
    assert retained_file.exists()
    adapter.fail_normalization = False

    retried = retry_quarantined_records(engine, raw_root, adapter, limit=10)

    assert first.records_failed == 1
    assert retried == RetryReport(source, attempted=1, records_saved=1, records_failed=0)
    with engine.connect() as connection:
        failures = connection.execute(
            text("SELECT count(*) FROM knowledge.sync_failure WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        document = connection.execute(
            text("SELECT document FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert failures == 0
    assert document == {"identifier": "VULN-RETRY", "summary": ""}


def test_retry_keeps_newer_quarantined_revision_after_older_one_succeeds(database):
    engine, source, raw_root = database

    @dataclass
    class VersionedSource(FixtureSource):
        failing_summaries: set[str]

        def normalize(self, document):
            if document["summary"] in self.failing_summaries:
                raise KeyError("adapter mapping is temporarily incomplete")
            return super().normalize(document)

    v1_raw = b'{"id":"VULN-VERSIONS","summary":"v1"}'
    v2_raw = b'{"id":"VULN-VERSIONS","summary":"v2"}'
    adapter = VersionedSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord(
                        "VULN-VERSIONS", v1_raw, datetime(2026, 1, 1, tzinfo=UTC)
                    ),
                    RawSourceRecord(
                        "VULN-VERSIONS", v2_raw, datetime(2026, 1, 2, tzinfo=UTC)
                    ),
                ),
                {"page": 1},
                True,
            )
        ],
        [],
        {"v1", "v2"},
    )

    first = ingest_source(engine, raw_root, adapter)
    assert first.records_failed == 2
    adapter.failing_summaries = {"v2"}

    retried = retry_quarantined_records(engine, raw_root, adapter, limit=10)

    assert retried == RetryReport(source, attempted=2, records_saved=1, records_failed=1)
    with engine.connect() as connection:
        failure = (
            connection.execute(
                text("""
                    SELECT sha256, raw_path, modified_at
                    FROM knowledge.sync_failure
                    WHERE source=:source AND external_id='VULN-VERSIONS'
                """),
                {"source": source},
            )
            .mappings()
            .one()
        )
        document = connection.execute(
            text("SELECT document FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert failure["sha256"] == digest(v2_raw)
    assert failure["raw_path"] is not None
    assert failure["modified_at"] == "2026-01-02T00:00:00+00:00"
    assert (raw_root / failure["raw_path"]).exists()
    assert document == {"identifier": "VULN-VERSIONS", "summary": "v1"}

    adapter.failing_summaries = set()
    retried_again = retry_quarantined_records(engine, raw_root, adapter, limit=10)

    assert retried_again == RetryReport(
        source, attempted=1, records_saved=1, records_failed=0
    )
    with engine.connect() as connection:
        failures = connection.execute(
            text("SELECT count(*) FROM knowledge.sync_failure WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        document = connection.execute(
            text("SELECT document FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert failures == 0
    assert document == {"identifier": "VULN-VERSIONS", "summary": "v2"}


def test_success_clears_prior_failures_for_same_id_and_invalid_alias(database):
    engine, source, raw_root = database

    @dataclass
    class ConditionalSource(FixtureSource):
        def normalize(self, document):
            if document.get("summary") == "broken":
                raise KeyError("broken adapter mapping")
            return super().normalize(document)

    good_raw = b'{"id":"VULN-RECOVERED","summary":"good"}'
    adapter = ConditionalSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord(
                        "VULN-RECOVERED",
                        b'{"id":"VULN-RECOVERED","summary":"broken"}',
                        datetime(2026, 1, 1, tzinfo=UTC),
                    ),
                    RawSourceRecord("\x00", good_raw),
                    RawSourceRecord(
                        "VULN-RECOVERED", good_raw, datetime(2026, 1, 2, tzinfo=UTC)
                    ),
                ),
                {"page": 1},
                True,
            )
        ],
        [],
    )

    report = ingest_source(engine, raw_root, adapter)

    assert report.records_saved == 1
    assert report.records_failed == 2
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM knowledge.sync_failure WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert count == 0


def test_jsonb_numeric_cursor_comparison_is_semantic(database):
    engine, source, raw_root = database
    cursor_value = 1.7e18
    adapter = FixtureSource(
        source,
        [
            SourcePage((), {"ts": cursor_value}, False),
            SourcePage((), {"complete": True}, True),
        ],
        [],
    )

    report = ingest_source(engine, raw_root, adapter, max_pages=2)

    assert report.status == "COMPLETE"
    assert adapter.calls[1][0] == {"ts": 1700000000000000000}


def test_jsonb_incompatible_cursor_is_reported_as_invalid(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [SourcePage((), {"token": "bad\x00cursor"}, False)],
        [],
    )

    with pytest.raises(InputError, match="KNOWLEDGE_SYNC_CURSOR_INVALID"):
        ingest_source(engine, raw_root, adapter)

    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, error_code FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {
        "status": "FAILED",
        "error_code": "KNOWLEDGE_SYNC_CURSOR_INVALID",
    }


def test_page_bytes_are_validated_before_any_raw_file_is_written(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord("VULN-FIRST", b'{"id":"VULN-FIRST"}'),
                    RawSourceRecord("VULN-LAST", "not bytes"),
                ),
                {"page": 1},
                True,
            )
        ],
        [],
    )

    with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_PAGE_INVALID"):
        ingest_source(engine, raw_root, adapter)

    assert list(raw_root.rglob("*.json")) == []


def test_local_storage_error_code_is_not_mislabeled_as_provider_error(database, monkeypatch):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (RawSourceRecord("VULN-LOCAL-ERROR", b'{"id":"VULN-LOCAL-ERROR"}'),),
                {"page": 1},
                True,
            )
        ],
        [],
    )

    def fail_local_save(*args, **kwargs):
        raise InputError("KNOWLEDGE_RAW_PATH_INVALID")

    monkeypatch.setattr(source_ingestion_module, "save_record", fail_local_save)

    with pytest.raises(InputError, match="KNOWLEDGE_RAW_PATH_INVALID"):
        ingest_source(engine, raw_root, adapter)

    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, error_code FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {"status": "FAILED", "error_code": "KNOWLEDGE_RAW_PATH_INVALID"}


def test_stale_modified_record_is_quarantined_and_cursor_advances(database):
    engine, source, raw_root = database
    adapter = FixtureSource(
        source,
        [
            SourcePage(
                (
                    RawSourceRecord(
                        "VULN-STALE",
                        b'{"id":"VULN-STALE","summary":"newer"}',
                        datetime(2026, 1, 2, tzinfo=UTC),
                    ),
                ),
                {},
                True,
            ),
            SourcePage(
                (RawSourceRecord("VULN-STALE", b'{"id":"VULN-STALE","summary":"older"}'),),
                {"page": 2},
                True,
            ),
        ],
        [],
    )
    ingest_source(engine, raw_root, adapter)

    report = ingest_source(engine, raw_root, adapter)

    assert report.records_saved == 0
    assert report.records_failed == 1
    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, cursor FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
        failure = connection.execute(
            text("SELECT error_code FROM knowledge.sync_failure WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        document = connection.execute(
            text("SELECT document FROM knowledge.source_record WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert state == {"status": "COMPLETE", "cursor": {"page": 2}}
    assert failure == "KNOWLEDGE_SOURCE_RECORD_STALE"
    assert document["summary"] == "newer"


def test_stale_running_state_is_recovered_before_fetch(database):
    engine, source, raw_root = database
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO knowledge.sync_state(source, cursor, status)
                VALUES (:source, CAST('{}' AS jsonb), 'RUNNING')
            """),
            {"source": source},
        )
    adapter = FixtureSource(source, [SourcePage((), {"page": 1}, True)], [])

    with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_RUN_INTERRUPTED"):
        ingest_source(engine, raw_root, adapter)

    assert adapter.calls == []
    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, error_code FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {
        "status": "FAILED",
        "error_code": "KNOWLEDGE_SOURCE_RUN_INTERRUPTED",
    }


def test_concurrent_source_run_is_rejected_before_second_fetch(database):
    engine, source, raw_root = database
    started = Event()
    release = Event()

    @dataclass
    class BlockingSource(FixtureSource):
        def fetch_page(self, cursor: Cursor, page_size: int) -> SourcePage:
            self.calls.append((cursor, page_size))
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test synchronization timed out")
            return self.pages.pop(0)

    adapter = BlockingSource(source, [SourcePage((), {"page": 1}, True)], [])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ingest_source, engine, raw_root, adapter)
        assert started.wait(timeout=5)
        try:
            with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_ALREADY_RUNNING"):
                ingest_source(engine, raw_root, adapter)
        finally:
            release.set()
        assert future.result(timeout=10).status == "COMPLETE"
    assert len(adapter.calls) == 1


def test_keyboard_interrupt_marks_run_failed(database):
    engine, source, raw_root = database

    class InterruptingSource(FixtureSource):
        def fetch_page(self, cursor: Cursor, page_size: int) -> SourcePage:
            raise KeyboardInterrupt

    adapter = InterruptingSource(source, [], [])

    with pytest.raises(KeyboardInterrupt):
        ingest_source(engine, raw_root, adapter)

    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, error_code FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {
        "status": "FAILED",
        "error_code": "KNOWLEDGE_SOURCE_RUN_INTERRUPTED",
    }


def test_unexpected_error_marks_run_failed(database, monkeypatch):
    engine, source, raw_root = database
    adapter = FixtureSource(source, [SourcePage((), {"page": 1}, True)], [])

    def fail_page(*args, **kwargs):
        raise ValueError("unexpected internal failure")

    monkeypatch.setattr(source_ingestion_module, "_commit_page", fail_page)

    with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_RUN_FAILED"):
        ingest_source(engine, raw_root, adapter)

    with engine.connect() as connection:
        state = (
            connection.execute(
                text("SELECT status, error_code FROM knowledge.sync_state WHERE source=:source"),
                {"source": source},
            )
            .mappings()
            .one()
        )
    assert state == {"status": "FAILED", "error_code": "KNOWLEDGE_SOURCE_RUN_FAILED"}
