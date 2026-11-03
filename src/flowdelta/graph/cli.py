"""A's shared-database commands; the database dependencies load only when a command runs."""

from pathlib import Path
from typing import Annotated

import typer

from flowdelta.ingestion import MAX_FILE_BYTES, InputError, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.pipeline_contracts import AnalysisBundle

app = typer.Typer(no_args_is_help=True)


def _read_analysis(path: Path) -> AnalysisBundle:
    try:
        with path.open("rb") as stream:
            # Bounded read, as for every other input file.
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise InputError("ANALYSIS_READ_FAILED") from None
    if len(data) > MAX_FILE_BYTES:
        raise InputError("FILE_SIZE_LIMIT")
    return parse_file_model(AnalysisBundle, parse_json(data))


def _exit_with_error(code: str) -> None:
    typer.echo(json_bytes({"error": code}).decode("utf-8"), err=True, nl=False)
    raise typer.Exit(2)


def _handle_missing_extra(exc: ModuleNotFoundError) -> None:
    if exc.name not in {"alembic", "psycopg", "sqlalchemy"}:
        raise exc
    _exit_with_error("KNOWLEDGE_EXTRA_REQUIRED")


@app.command("migrate")
def migrate_command() -> None:
    """Create or upgrade A's graph.analysis table with the migration role."""
    try:
        from flowdelta.graph.database import migrate_analyses

        migrate_analyses(KnowledgeSettings.from_environment())
        typer.echo("ANALYSIS_DB_MIGRATED")
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)


@app.command("store")
def store_command(analysis: Path) -> None:
    """Store one analysis.json; the same content again is a no-op, other content is refused."""
    try:
        from flowdelta.graph.database import DatabaseAnalysisRepository
        from flowdelta.knowledge.database import engine_for

        bundle = _read_analysis(analysis)
        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            record = DatabaseAnalysisRepository(engine).save(bundle)
        finally:
            engine.dispose()
        typer.echo(
            json_bytes(
                {
                    "status": "ANALYSIS_STORED",
                    "analysis_id": record.analysis_id,
                    "sha256": record.sha256,
                }
            ).decode("utf-8"),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)


@app.command("get")
def get_command(
    analysis_id: str,
    out: Annotated[Path, typer.Option(help="New analysis.json path; parent must exist.")],
) -> None:
    """Write the stored analysis with exactly this ID; never the latest one of a case."""
    try:
        from flowdelta.graph.database import DatabaseAnalysisRepository
        from flowdelta.knowledge.database import engine_for

        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            bundle = DatabaseAnalysisRepository(engine).get(analysis_id)
        finally:
            engine.dispose()
        try:
            with out.open("xb") as file:
                file.write(json_bytes(bundle.model_dump(mode="json")))
        except FileExistsError:
            raise InputError("OUTPUT_EXISTS") from None
        except OSError:
            raise InputError("OUTPUT_WRITE_FAILED") from None
        typer.echo(
            json_bytes({"status": "ANALYSIS_WRITTEN", "analysis_id": analysis_id}).decode("utf-8"),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
