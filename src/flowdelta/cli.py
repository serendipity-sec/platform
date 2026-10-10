"""Offline CLI for the first team handoff."""

from pathlib import Path
from typing import Annotated

import typer

from flowdelta import SCHEMA_VERSION, __version__
from flowdelta.contracts import Handoff
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command()
def version() -> None:
    """Print package and contract versions."""
    typer.echo(f"flowdelta {__version__}; schema {SCHEMA_VERSION}")


@app.command()
def schema() -> None:
    """Print JSON Schema; file contract types are under $defs."""
    typer.echo(json_bytes(Handoff.model_json_schema()).decode("utf-8"), nl=False)


@app.command("inspect-case")
def inspect_case(bundle: Path) -> None:
    """Read and structurally parse a local bundle without executing its workflow."""
    try:
        loaded = load_case(bundle)
        handoff = build_handoff(loaded)
        typer.echo(
            json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "case_id": loaded.case.case_id,
                    "processing_status": "STRUCTURE_PARSED",
                    "semantic_support": "UNKNOWN",
                    "execution_status": "NOT_RUN",
                    "before_nodes": len(handoff.before.nodes),
                    "after_nodes": len(handoff.after.nodes),
                    "before_connections": len(handoff.before.connections),
                    "after_connections": len(handoff.after.connections),
                    "artifact_count": len(loaded.manifest.artifacts),
                    "obligation_count": len(loaded.case.obligations),
                }
            ).decode("utf-8"),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes({"error": exc.code}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command()
def snapshot(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="New output directory; parent must exist.")],
) -> None:
    """Write versioned files for A and C to a fresh local directory."""
    try:
        loaded = load_case(bundle)
        handoff = build_handoff(loaded)
        write_handoff(loaded, handoff, out)
        typer.echo(
            json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "HANDOFF_WRITTEN",
                    "case_id": loaded.case.case_id,
                    "execution_status": "NOT_RUN",
                }
            ).decode("utf-8"),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes({"error": exc.code}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()
