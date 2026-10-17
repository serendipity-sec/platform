"""Knowledge operations; optional DB dependencies load only when requested."""

import typer

from flowdelta.ingestion import InputError, json_bytes
from flowdelta.knowledge.settings import KnowledgeSettings

app = typer.Typer(no_args_is_help=True)


@app.command("migrate")
def migrate_command() -> None:
    from flowdelta.knowledge.database import migrate

    try:
        migrate(KnowledgeSettings.from_environment())
        typer.echo("KNOWLEDGE_MIGRATED")
    except InputError as exc:
        typer.echo(exc.code, err=True)
        raise typer.Exit(2) from None


@app.command("status")
def status_command() -> None:
    from flowdelta.knowledge.database import database_status

    try:
        typer.echo(json_bytes(database_status(KnowledgeSettings.from_environment())).decode())
    except InputError as exc:
        typer.echo(exc.code, err=True)
        raise typer.Exit(2) from None
