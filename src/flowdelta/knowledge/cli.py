"""Knowledge operations; optional DB dependencies load only when requested."""

from pathlib import Path
from typing import Annotated

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


@app.command("store-plan")
def store_plan_command(plan: Path) -> None:
    """Store a generated plan and its referenced retrieval/KB bytes in PostgreSQL."""
    engine = None
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.knowledge.repository import store_b_outputs

        engine = engine_for(KnowledgeSettings.from_environment())
        bundle = store_b_outputs(engine, plan)
        typer.echo(
            json_bytes(
                {
                    "status": "B_OUTPUTS_STORED",
                    "plan_id": bundle.plan.plan_id,
                    "retrieval_id": bundle.retrieval.retrieval_id,
                    "analysis_id": bundle.plan.analysis_id,
                }
            ).decode(),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    finally:
        if engine is not None:
            engine.dispose()


@app.command("index-cards")
def index_cards_command(
    bundle: Path,
    cards: Annotated[Path, typer.Option(help="Local card directory to add to the shared catalog.")],
) -> None:
    """Index bounded local cards using the exact profile of the running embedding model."""
    engine = None
    try:
        from flowdelta.knowledge.card_catalog import MAX_INDEX_BATCH_SIZE, index_cards
        from flowdelta.knowledge.cards import load_cards
        from flowdelta.knowledge.database import engine_for
        from flowdelta.knowledge.embedding_settings import EmbeddingSettings

        loaded_cards = load_cards(cards, bundle_root=bundle.resolve())
        embedding_settings = EmbeddingSettings.from_environment()
        provider = embedding_settings.provider()
        profile = provider.profile()
        engine = engine_for(KnowledgeSettings.from_environment())
        result = index_cards(
            engine,
            loaded_cards,
            provider,
            profile,
            batch_size=min(embedding_settings.max_batch_size, MAX_INDEX_BATCH_SIZE),
        )
        typer.echo(
            json_bytes(
                {"status": "CARDS_INDEXED", "indexed": result.indexed, "reused": result.reused}
            ).decode("utf-8"),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    except OSError:
        _exit_with_error("CARD_CATALOG_INDEX_FAILED")
    finally:
        if engine is not None:
            engine.dispose()


@app.command("check-evidence")
def check_evidence_command(
    cards: Path,
    root: Annotated[
        Path, typer.Option(help="Directory that card evidence artifact paths are relative to.")
    ] = Path("."),
) -> None:
    """Check that card claim evidence references resolve, match hashes and support claims.

    Exit 0: no failed reference. Exit 1: at least one FAILED reference. Exit 2: unreadable cards.
    Pinned upstream sources outside the checkout are reported as SOURCE_UNAVAILABLE.
    """
    from flowdelta.knowledge.card_evidence import FAILED, check_cards
    from flowdelta.knowledge.cards import load_cards

    try:
        report = check_cards(load_cards(cards, bundle_root=root.resolve()), root)
        typer.echo(json_bytes(report).decode("utf-8"), nl=False)
    except InputError as exc:
        _exit_with_error(exc.code)
        return
    totals = report["totals"]
    if isinstance(totals, dict) and totals.get(FAILED):
        raise typer.Exit(1)


@app.command("export-plan")
def export_plan_command(
    plan_id: str,
    out: Annotated[Path, typer.Option(help="New directory for plan.json and its B artifacts.")],
) -> None:
    """Load one exact plan ID and materialize its original B artifact bytes."""
    engine = None
    try:
        from flowdelta.knowledge.database import engine_for
        from flowdelta.knowledge.repository import export_b_outputs, load_b_outputs

        engine = engine_for(KnowledgeSettings.from_environment())
        bundle = load_b_outputs(engine, plan_id)
        if bundle is None:
            raise InputError("KNOWLEDGE_PLAN_NOT_FOUND")
        export_b_outputs(bundle, out)
        typer.echo(
            json_bytes({"status": "B_OUTPUTS_EXPORTED", "plan_id": bundle.plan.plan_id}).decode(),
            nl=False,
        )
    except ModuleNotFoundError as exc:
        _handle_missing_extra(exc)
    except InputError as exc:
        _exit_with_error(exc.code)
    finally:
        if engine is not None:
            engine.dispose()
