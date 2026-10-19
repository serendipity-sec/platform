import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.database import database_status, engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.storage import (
    cleanup_orphaned_raw,
    save_raw,
    save_record,
    storage_transaction,
)


class _StatusResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _StatusConnection:
    def __init__(self, vector, has_migrations, revision):
        self.vector = vector
        self.has_migrations = has_migrations
        self.revision = revision

    def execute(self, statement):
        sql = str(statement)
        if "SHOW server_version" in sql:
            return _StatusResult("17.8")
        if "FROM pg_extension" in sql:
            return _StatusResult(self.vector)
        if "to_regclass" in sql:
            return _StatusResult(self.has_migrations)
        if "FROM alembic_version" in sql:
            return _StatusResult(self.revision)
        raise AssertionError(f"unexpected status query: {sql}")


class _StatusEngine:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection

    def dispose(self):
        pass


def test_database_status_reports_uninitialized_database(monkeypatch):
    import flowdelta.knowledge.database as database

    settings = KnowledgeSettings(database_url="postgresql+psycopg://user:pass@localhost/db")
    engine = _StatusEngine(_StatusConnection(vector=None, has_migrations=None, revision=None))
    monkeypatch.setattr(database, "engine_for", lambda _: engine)
    with pytest.raises(InputError, match="KNOWLEDGE_VECTOR_EXTENSION_MISSING"):
        database.database_status(settings)

    engine = _StatusEngine(
        _StatusConnection(vector="0.8.1", has_migrations=None, revision=None)
    )
    monkeypatch.setattr(database, "engine_for", lambda _: engine)
    with pytest.raises(InputError, match="KNOWLEDGE_MIGRATION_REQUIRED"):
        database.database_status(settings)


def test_raw_idempotent_and_integrity(tmp_path):
    path, checksum = save_raw(tmp_path, b'{"fixture":true}')
    assert save_raw(tmp_path, b'{"fixture":true}') == (path, checksum)
    (tmp_path / path).write_bytes(b"corrupted")
    with pytest.raises(InputError, match="KNOWLEDGE_RAW_INTEGRITY_MISMATCH"):
        save_raw(tmp_path, b'{"fixture":true}')


def test_settings_hide_credentials():
    settings = KnowledgeSettings(database_url="postgresql+psycopg://user:secret@localhost/db")
    assert "secret" not in repr(settings)


@pytest.mark.skipif(
    not os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable PostgreSQL database",
)
def test_postgres_migration_and_transaction(tmp_path):
    settings = KnowledgeSettings(
        database_url=os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"],
        migration_url=os.environ.get(
            "FLOWDELTA_KB_TEST_MIGRATION_URL", os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"]
        ),
        raw_root=tmp_path,
    )
    migrate(settings)
    migrate(settings)
    assert database_status(settings)["pgvector"] == "0.8.1"
    engine = engine_for(settings)
    migration_engine = engine_for(settings, migration=True)
    try:
        with engine.connect() as connection:
            is_superuser = connection.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar_one()
            if not is_superuser:
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'knowledge.source_revision', 'UPDATE')"
                    )
                ).scalar_one()
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'knowledge.source_revision', 'DELETE')"
                    )
                ).scalar_one()

        def clear_source(source: str) -> None:
            with migration_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM knowledge.source_record WHERE source=:source"),
                    {"source": source},
                )
                connection.execute(
                    text("DELETE FROM knowledge.source_revision WHERE source=:source"),
                    {"source": source},
                )

        class RollbackFixture(Exception):
            pass

        try:
            with storage_transaction(engine) as connection:
                save_record(
                    connection,
                    tmp_path,
                    "rollback-test",
                    "fixture",
                    json_bytes({"revision": 1}),
                )
                raise RollbackFixture
        except RollbackFixture:
            pass
        assert list(tmp_path.rglob("*.json")) == []
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM knowledge.source_record WHERE source='rollback-test'"
                    )
                ).scalar_one()
                == 0
            )
        with storage_transaction(engine) as connection:
            first = save_record(
                connection, tmp_path, "test", "fixture", json_bytes({"revision": 1})
            )
            assert first == save_record(
                connection, tmp_path, "test", "fixture", json_bytes({"revision": 1})
            )
            second = save_record(
                connection, tmp_path, "test", "fixture", json_bytes({"revision": 2})
            )
            assert first != second
            count = connection.execute(
                text("SELECT count(*) FROM knowledge.source_revision WHERE source='test'")
            ).scalar_one()
            assert count == 2
        clear_source("test")
        assert list(tmp_path.rglob("*.json")) == []

        raw_payload = b'{ "revision": 7.00 }\r\n'
        with storage_transaction(engine) as connection:
            checksum = save_record(
                connection, tmp_path, "raw-bytes-test", "fixture", raw_payload
            )
        raw_path = tmp_path / checksum[:2] / f"{checksum}.json"
        assert checksum == digest(raw_payload)
        assert raw_path.read_bytes() == raw_payload
        clear_source("raw-bytes-test")
        assert not raw_path.exists()

        with pytest.raises(InputError, match="KNOWLEDGE_MODIFIED_AT_TIMEZONE_REQUIRED"):
            with storage_transaction(engine) as connection:
                save_record(
                    connection,
                    tmp_path,
                    "timezone-test",
                    "fixture",
                    json_bytes({"revision": 1}),
                    modified_at="2026-01-01T00:00:00",
                )

        with storage_transaction(engine) as connection:
            current_checksum = save_record(
                connection,
                tmp_path,
                "stale-test",
                "fixture",
                json_bytes({"revision": 2}),
                modified_at="2026-01-02T00:00:00+00:00",
            )
        stale_payload = json_bytes({"revision": 1})
        with pytest.raises(InputError, match="KNOWLEDGE_SOURCE_RECORD_STALE"):
            with storage_transaction(engine) as connection:
                save_record(
                    connection,
                    tmp_path,
                    "stale-test",
                    "fixture",
                    stale_payload,
                    modified_at="2026-01-01T00:00:00+00:00",
                )
        stale_path = tmp_path / digest(stale_payload)[:2] / f"{digest(stale_payload)}.json"
        assert not stale_path.exists()
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT sha256 FROM knowledge.source_record "
                    "WHERE source='stale-test' AND external_id='fixture'"
                )
            ).scalar_one().strip() == current_checksum
        clear_source("stale-test")

        with storage_transaction(engine) as connection:
            first = save_record(
                connection,
                tmp_path,
                "cleanup-test",
                "fixture",
                json_bytes({"revision": 1}),
            )
        referenced_path = tmp_path / first[:2] / f"{first}.json"
        orphan_relative, _ = save_raw(tmp_path, b'{"fixture":"orphan"}')
        orphan_path = tmp_path / orphan_relative
        fresh_relative, _ = save_raw(tmp_path, b'{"fixture":"fresh"}')
        fresh_path = tmp_path / fresh_relative
        old_timestamp = time.time() - 10
        os.utime(referenced_path, (old_timestamp, old_timestamp))
        os.utime(orphan_path, (old_timestamp, old_timestamp))
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert referenced_path.exists()
        assert not orphan_path.exists()
        assert fresh_path.exists()
        os.utime(fresh_path, (old_timestamp, old_timestamp))
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert not fresh_path.exists()

        clear_source("cleanup-test")
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert not referenced_path.exists()

        def save_same_revision(_: int) -> str:
            with storage_transaction(engine) as connection:
                return save_record(
                    connection,
                    tmp_path,
                    "concurrency-test",
                    "fixture",
                    json_bytes({"revision": 1}),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            checksums = list(executor.map(save_same_revision, range(2)))
        assert checksums[0] == checksums[1]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM knowledge.source_revision "
                        "WHERE source='concurrency-test'"
                    )
                ).scalar_one()
                == 1
            )
        concurrent_path = tmp_path / checksums[0][:2] / f"{checksums[0]}.json"
        os.utime(concurrent_path, (old_timestamp, old_timestamp))
        clear_source("concurrency-test")
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert not concurrent_path.exists()
    finally:
        engine.dispose()
        migration_engine.dispose()
