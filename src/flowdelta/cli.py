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
                    "semantic_support": handoff.before.semantic_support
                    if handoff.before.semantic_support == handoff.after.semantic_support
                    else "PARTIAL",
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


@app.command("baseline")
def baseline(
    out: Annotated[Path, typer.Option(help="New directory below this repository's var/.")],
) -> None:
    """Run the registered normal order baseline in isolated local Docker environments."""
    from flowdelta.baseline.manual import BaselineError, run_baseline

    try:
        result = run_baseline(Path.cwd(), out)
        typer.echo(json_bytes(result).decode("utf-8"), nl=False)
        if not result["normal_baseline_complete"]:
            raise typer.Exit(code=2)
    except (BaselineError, InputError) as exc:
        typer.echo(json_bytes({"error": str(exc)}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("pipeline-schema")
def pipeline_schema() -> None:
    """Print the common A/B/C contracts and scenario card schema."""
    from flowdelta.pipeline_contracts import PipelineExamples

    typer.echo(json_bytes(PipelineExamples.model_json_schema()).decode("utf-8"), nl=False)


@app.command("contract-examples")
def contract_examples(
    out: Annotated[Path, typer.Option(help="Fresh reference example directory.")],
) -> None:
    """Write team-authored reference files; does not run analysis, planning or Docker."""
    from flowdelta.contract_examples import write_contract_examples

    try:
        write_contract_examples(Path.cwd(), out)
        typer.echo("CONTRACT_EXAMPLES_WRITTEN; execution_status=NOT_RUN")
    except InputError as exc:
        typer.echo(json_bytes({"error": exc.code}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("check-examples")
def check_examples(bundle: Path) -> None:
    """Check bytes, contracts and references in a team-authored example bundle."""
    from flowdelta.pipeline_io import read_pipeline_examples

    try:
        read_pipeline_examples(bundle)
        typer.echo("CONTRACT_EXAMPLES_VALID; execution_status=NOT_RUN")
    except InputError as exc:
        typer.echo(json_bytes({"error": exc.code}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()
