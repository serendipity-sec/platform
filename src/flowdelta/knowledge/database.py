"""PostgreSQL connections and explicit schema migration; no implicit startup DDL."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError
from flowdelta.knowledge.settings import KnowledgeSettings


def engine_for(settings: KnowledgeSettings, *, migration: bool = False) -> Engine:
    secret = settings.migration_url if migration else settings.database_url
    if secret is None:
        raise InputError("KNOWLEDGE_MIGRATION_URL_REQUIRED")
    url = secret.get_secret_value()
    if not url.startswith("postgresql+psycopg://"):
        raise InputError("KNOWLEDGE_POSTGRES_REQUIRED")
    return create_engine(
        url,
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 10},
    )


def migrate(settings: KnowledgeSettings) -> None:
    engine = engine_for(settings, migration=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except (CommandError, OSError, SQLAlchemyError):
        raise InputError("KNOWLEDGE_MIGRATION_FAILED") from None
    finally:
        engine.dispose()


def database_status(settings: KnowledgeSettings) -> dict[str, str]:
    engine = engine_for(settings)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            vector = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            ).scalar_one_or_none()
            if vector is None:
                raise InputError("KNOWLEDGE_VECTOR_EXTENSION_MISSING")
            has_migrations = connection.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar_one()
            if has_migrations is None:
                raise InputError("KNOWLEDGE_MIGRATION_REQUIRED")
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            if revision is None:
                raise InputError("KNOWLEDGE_MIGRATION_REQUIRED")
            return {"postgres": str(version), "pgvector": str(vector), "migration": str(revision)}
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None
    finally:
        engine.dispose()
