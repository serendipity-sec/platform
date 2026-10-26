"""PostgreSQL analysis repository on the shared database settings (knowledge extra).

Same protocol and rules as the file repository: an analysis ID holds exactly one content,
rows are never updated or deleted, and every read checks the stored hash. A keeps its own
migration chain and version table, so it never interleaves with the knowledge migrations.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.graph.repository import AnalysisRecord, _checked_id, _decode, _encode, _record
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.database import engine_for
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.pipeline_contracts import AnalysisBundle

_INSERT = text("""
    INSERT INTO graph.analysis (
        analysis_id, case_id, before_snapshot_id, after_snapshot_id,
        producer, origin, sha256, payload, limitations, extensions
    )
    VALUES (
        :analysis_id, :case_id, :before_snapshot_id, :after_snapshot_id,
        :producer, :origin, :sha256, :payload,
        CAST(:limitations AS jsonb), CAST(:extensions AS jsonb)
    )
    ON CONFLICT (analysis_id) DO NOTHING
    RETURNING analysis_id
""")


def migrate_analyses(settings: KnowledgeSettings) -> None:
    """Apply A's migrations with the migration role; the app role only reads and inserts."""
    engine = engine_for(settings, migration=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except (CommandError, OSError, SQLAlchemyError):
        raise InputError("ANALYSIS_MIGRATION_FAILED") from None
    finally:
        engine.dispose()


def _verified(analysis_id: str, payload: bytes, checksum: str) -> AnalysisBundle:
    if digest(payload) != checksum:
        raise InputError("ANALYSIS_INTEGRITY_MISMATCH", location=analysis_id)
    return _decode(payload, analysis_id)


class DatabaseAnalysisRepository:
    """`graph.analysis` rows; the bundle bytes are stored as written and hashed on every read."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, bundle: AnalysisBundle) -> AnalysisRecord:
        analysis_id = _checked_id(bundle.analysis_id)
        payload = _encode(bundle)
        checksum = digest(payload)
        values = {
            "analysis_id": analysis_id,
            "case_id": bundle.case_id,
            "before_snapshot_id": bundle.before_snapshot_id,
            "after_snapshot_id": bundle.after_snapshot_id,
            "producer": bundle.authorship.producer,
            "origin": bundle.authorship.origin,
            "sha256": checksum,
            "payload": payload,
            "limitations": json_bytes(list(bundle.limitations)).decode("utf-8"),
            "extensions": json_bytes(dict(bundle.extensions)).decode("utf-8"),
        }
        try:
            with self._engine.begin() as connection:
                if connection.execute(_INSERT, values).first() is None:
                    # Another save holds the ID; the same content is a no-op, other is refused.
                    stored = connection.execute(
                        text("SELECT sha256 FROM graph.analysis WHERE analysis_id = :id"),
                        {"id": analysis_id},
                    ).scalar_one()
                    if stored != checksum:
                        raise InputError("ANALYSIS_ID_CONFLICT", location=analysis_id)
        except SQLAlchemyError:
            raise InputError("ANALYSIS_DATABASE_UNAVAILABLE") from None
        return _record(bundle, checksum, "")

    def get(self, analysis_id: str) -> AnalysisBundle:
        _checked_id(analysis_id)
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    text("SELECT payload, sha256 FROM graph.analysis WHERE analysis_id = :id"),
                    {"id": analysis_id},
                ).first()
        except SQLAlchemyError:
            raise InputError("ANALYSIS_DATABASE_UNAVAILABLE") from None
        if row is None:
            raise InputError("ANALYSIS_NOT_FOUND", location=analysis_id)
        return _verified(analysis_id, bytes(row.payload), row.sha256)

    def find(
        self, case_id: str, before_snapshot_id: str, after_snapshot_id: str
    ) -> list[AnalysisRecord]:
        """Exact case and snapshot match, never "the latest analysis of the case"."""
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    text("""
                        SELECT analysis_id, payload, sha256 FROM graph.analysis
                        WHERE case_id = :case_id
                          AND before_snapshot_id = :before
                          AND after_snapshot_id = :after
                        ORDER BY analysis_id
                    """),
                    {"case_id": case_id, "before": before_snapshot_id, "after": after_snapshot_id},
                ).all()
        except SQLAlchemyError:
            raise InputError("ANALYSIS_DATABASE_UNAVAILABLE") from None
        return [
            _record(_verified(row.analysis_id, bytes(row.payload), row.sha256), row.sha256, "")
            for row in rows
        ]
