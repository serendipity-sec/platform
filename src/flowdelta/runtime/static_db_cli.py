"""Static scan result commands on the shared database.

DB dependencies load only when a command runs.
"""

from pathlib import Path
from typing import Annotated

import typer

from flowdelta.ingestion import MAX_FILE_BYTES, InputError, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.runtime.static_scan import StaticScanRun

app = typer.Typer(no_args_is_help=True)


def _read_run(path: Path) -> StaticScanRun:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise InputError("STATIC_RUN_READ_FAILED") from None
    if len(data) > MAX_FILE_BYTES:
        raise InputError("FILE_SIZE_LIMIT")
    return parse_file_model(StaticScanRun, parse_json(data))


def _write_new(out: Path, value: object) -> None:
    try:
        with out.open("xb") as file:
            file.write(json_bytes(value))
    except FileExistsError:
        raise InputError("OUTPUT_EXISTS") from None
    except OSError:
        raise InputError("OUTPUT_WRITE_FAILED") from None


def _exit_with_error(code: str) -> None:
    typer.echo(json_bytes({"error": code}).decode("utf-8"), err=True, nl=False)
    raise typer.Exit(2)


def _handle_missing_extra(exc: ModuleNotFoundError) -> None:
    if exc.name not in {"alembic", "psycopg", "sqlalchemy"}:
        raise exc
    _exit_with_error("KNOWLEDGE_EXTRA_REQUIRED")


@app.command("migrate")
def migrate_command() -> None:
    """Create or upgrade the static scan result table with the migration role."""
    try:
        from flowdelta.runtime.static_database import migrate_static_runs

        migrate_static_runs(KnowledgeSettings.from_environment())
        typer.echo("STATIC_DB_MIGRATED")
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)


@app.command("store")
def store_command(run: Path) -> None:
    """Store one *.static-run.json.

    Saving the same content again is allowed; other content for the same ID is refused.
    """
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.runtime.static_database import DatabaseStaticRunRepository

        value = _read_run(run)
        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            DatabaseStaticRunRepository(engine).save(value)
        finally:
            engine.dispose()
        typer.echo(
            json_bytes({"status": "STATIC_RUN_STORED", "scan_run_id": value.run_id}).decode(
                "utf-8"
            ),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)


@app.command("get")
def get_command(
    scan_run_id: str,
    out: Annotated[Path, typer.Option(help="New JSON file path; the parent folder must exist.")],
) -> None:
    """Write the stored result with exactly this scan run ID."""
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.runtime.static_database import DatabaseStaticRunRepository

        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            value = DatabaseStaticRunRepository(engine).get(scan_run_id)
        finally:
            engine.dispose()
        _write_new(out, value.model_dump(mode="json"))
        typer.echo(
            json_bytes({"status": "STATIC_RUN_WRITTEN", "scan_run_id": scan_run_id}).decode(
                "utf-8"
            ),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)


@app.command("list")
def list_command(
    plan_id: str,
    out: Annotated[Path, typer.Option(help="New JSON file path; the parent folder must exist.")],
    comparison_run_id: Annotated[
        str | None, typer.Option(help="Only runs of this comparison run.")
    ] = None,
) -> None:
    """Write every stored run of this exact plan ID as one JSON array.

    Never picks a latest result.
    """
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.runtime.static_database import DatabaseStaticRunRepository

        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            runs = DatabaseStaticRunRepository(engine).list_by_plan(plan_id, comparison_run_id)
        finally:
            engine.dispose()
        _write_new(out, [run.model_dump(mode="json") for run in runs])
        typer.echo(
            json_bytes(
                {"status": "STATIC_RUNS_WRITTEN", "plan_id": plan_id, "count": len(runs)}
            ).decode("utf-8"),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
