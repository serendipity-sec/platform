"""Knowledge operations; optional DB dependencies load only when requested."""

import typer

from flowdelta.ingestion import InputError, json_bytes
from flowdelta.knowledge.settings import KnowledgeSettings

app = typer.Typer(no_args_is_help=True)


def _exit_with_error(code: str) -> None:
    typer.echo(json_bytes({"error": code}).decode("utf-8"), err=True, nl=False)
    raise typer.Exit(2)


def _handle_missing_extra(exc: ModuleNotFoundError) -> None:
    if exc.name not in {"alembic", "psycopg", "sqlalchemy"}:
        raise exc
    _exit_with_error("KNOWLEDGE_EXTRA_REQUIRED")


@app.command("migrate")
def migrate_command() -> None:
    try:
        from flowdelta.knowledge.database import migrate

        migrate(KnowledgeSettings.from_environment())
        typer.echo("KNOWLEDGE_MIGRATED")
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    except OSError:
        _exit_with_error("KNOWLEDGE_MIGRATION_FAILED")


@app.command("status")
def status_command() -> None:
    try:
        from flowdelta.knowledge.database import database_status

        status = database_status(KnowledgeSettings.from_environment())
        typer.echo(json_bytes(status).decode("utf-8"), nl=False)
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    except OSError:
        _exit_with_error("KNOWLEDGE_DATABASE_UNAVAILABLE")


@app.command("cleanup-raw")
def cleanup_raw_command() -> None:
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.knowledge.storage import cleanup_orphaned_raw

        settings = KnowledgeSettings.from_environment()
        engine = engine_for(settings)
        try:
            removed = cleanup_orphaned_raw(engine, settings.raw_root)
        finally:
            engine.dispose()
        typer.echo(json_bytes({"removed": removed}).decode("utf-8"), nl=False)
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    except OSError:
        _exit_with_error("KNOWLEDGE_RAW_CLEANUP_FAILED")
