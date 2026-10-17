import os

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text

from flowdelta.ingestion import InputError
from flowdelta.knowledge.database import database_status, engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.storage import save_raw, save_record


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
        with engine.connect() as connection:
            transaction = connection.begin()
            save_record(connection, tmp_path, "rollback-test", "fixture", {"revision": 1})
            transaction.rollback()
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM knowledge.source_record WHERE source='rollback-test'"
                    )
                ).scalar_one()
                == 0
            )
        with engine.begin() as connection:
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
    finally:
        engine.dispose()
