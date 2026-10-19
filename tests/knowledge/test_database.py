import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text

from flowdelta.ingestion import InputError
from flowdelta.knowledge.database import database_status, engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.storage import (
    cleanup_orphaned_raw,
    save_raw,
    save_record,
    storage_transaction,
)


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
    try:

        class RollbackFixture(Exception):
            pass

        try:
            with storage_transaction(engine) as connection:
                save_record(connection, tmp_path, "rollback-test", "fixture", {"revision": 1})
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
            first = save_record(connection, tmp_path, "test", "fixture", {"revision": 1})
            assert first == save_record(connection, tmp_path, "test", "fixture", {"revision": 1})
            second = save_record(connection, tmp_path, "test", "fixture", {"revision": 2})
            assert first != second
            count = connection.execute(
                text("SELECT count(*) FROM knowledge.source_revision WHERE source='test'")
            ).scalar_one()
            assert count == 2
            connection.execute(text("DELETE FROM knowledge.source_record WHERE source='test'"))
            connection.execute(text("DELETE FROM knowledge.source_revision WHERE source='test'"))
        assert list(tmp_path.rglob("*.json")) == []

        with storage_transaction(engine) as connection:
            first = save_record(connection, tmp_path, "cleanup-test", "fixture", {"revision": 1})
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

        with storage_transaction(engine) as connection:
            connection.execute(
                text("DELETE FROM knowledge.source_record WHERE source='cleanup-test'")
            )
            connection.execute(
                text("DELETE FROM knowledge.source_revision WHERE source='cleanup-test'")
            )
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert not referenced_path.exists()

        def save_same_revision(_: int) -> str:
            with storage_transaction(engine) as connection:
                return save_record(
                    connection,
                    tmp_path,
                    "concurrency-test",
                    "fixture",
                    {"revision": 1},
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
        with storage_transaction(engine) as connection:
            connection.execute(
                text("DELETE FROM knowledge.source_record WHERE source='concurrency-test'")
            )
            connection.execute(
                text("DELETE FROM knowledge.source_revision WHERE source='concurrency-test'")
            )
        assert cleanup_orphaned_raw(engine, tmp_path, older_than_seconds=1) == 1
        assert not concurrent_path.exists()
    finally:
        engine.dispose()
