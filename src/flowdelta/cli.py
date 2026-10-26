"""Offline CLI for the first team handoff."""

from pathlib import Path
from typing import Annotated

import typer

from flowdelta import SCHEMA_VERSION, __version__
from flowdelta.contracts import Handoff
from flowdelta.graph.cli import app as analysis_db_app
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.knowledge.cli import app as knowledge_app

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(analysis_db_app, name="analysis-db")


@app.command()
def version() -> None:
    """Print package and contract versions."""
    typer.echo(f"flowdelta {__version__}; schema {SCHEMA_VERSION}")


@app.command("embedding-smoke")
def embedding_smoke() -> None:
    """Create safe local sample vectors and report profile metadata, never vector contents."""
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    try:
        provider = EmbeddingSettings.from_environment().provider()
        document = provider.embed(
            ["Synthetic advisory record: affected component version 1.2.0."], "DOCUMENT"
        )
        query = provider.embed(["Synthetic workflow change may expose an internal memo."], "QUERY")
        if document.profile.model_digest != query.profile.model_digest:
            raise InputError("EMBEDDING_MODEL_REVISION_CHANGED")
        typer.echo(
            json_bytes(
                {
                    "status": "EMBEDDING_READY",
                    "provider": query.profile.provider,
                    "model": query.profile.model,
                    "model_digest": query.profile.model_digest,
                    "dimensions": query.profile.dimensions,
                    "document_profile_id": document.profile.profile_id,
                    "query_profile_id": query.profile.profile_id,
                    "document_vectors": len(document.vectors),
                    "query_vectors": len(query.vectors),
                    "sample_content_emitted": False,
                    "vector_contents_emitted": False,
                }
            ).decode(),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


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


@app.command("analyze")
def analyze_handoff(
    handoff: Path,
    out: Annotated[Path, typer.Option(help="New analysis.json path; parent must exist.")],
) -> None:
    """A: write a static control graph and change list; does not run the workflow."""
    from flowdelta.graph import analyze
    from flowdelta.pipeline_io import read_handoff

    try:
        bundle = analyze(read_handoff(handoff))
        try:
            with out.open("xb") as file:
                file.write(json_bytes(bundle.model_dump(mode="json")))
        except FileExistsError:
            raise InputError("OUTPUT_EXISTS") from None
        except OSError:
            raise InputError("OUTPUT_WRITE_FAILED") from None
        typer.echo(
            json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "ANALYSIS_WRITTEN",
                    "analysis_id": bundle.analysis_id,
                    "changes": len(bundle.changes),
                    "execution_status": "NOT_RUN",
                }
            ).decode("utf-8"),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode("utf-8"), err=True, nl=False)
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


@app.command("migrate-case")
def migrate_case(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="Fresh directory for the converted 0.4.0 bundle.")],
) -> None:
    """Explicitly migrate source metadata to 0.4.0; preserve raw workflow bytes."""
    from flowdelta.migration import migrate_case_03

    try:
        migrate_case_03(bundle, out)
        typer.echo("CASE_MIGRATED; execution_status=NOT_RUN")
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("pipeline-schema")
def pipeline_schema() -> None:
    """Print the common A/B/C contracts and scenario card schema."""
    from flowdelta.pipeline_contracts import PipelineExamples

    typer.echo(json_bytes(PipelineExamples.model_json_schema()).decode("utf-8"), nl=False)


@app.command("check-pipeline")
def check_pipeline(bundle: Path) -> None:
    """Check real module output files and, when supplied, C's runtime artifact references."""
    from flowdelta.module_outputs import read_module_outputs

    try:
        typer.echo(json_bytes(read_module_outputs(bundle)).decode("utf-8"), nl=False)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("conformance")
def conformance(
    out: Annotated[Path, typer.Option(help="Fresh directory below repository var/.")],
) -> None:
    """Run the registered normal node fixtures and compare extracted facts with n8n."""
    from flowdelta.conformance.compare import compare_case
    from flowdelta.conformance.lab import run_conformance

    try:
        repo = Path.cwd()
        result = run_conformance(repo, out)
        reports = [
            compare_case(repo, folder) for folder in sorted(out.iterdir()) if folder.is_dir()
        ]
        summary = {
            "schema_version": SCHEMA_VERSION,
            "cases": len(reports),
            "all_observed_checks_match": all(
                report["all_observed_checks_match"] for report in reports
            ),
            "checks": sum(len(report["checks"]) for report in reports),
            "outcome": None,
        }
        (out / "summary.json").write_bytes(json_bytes(summary))
        typer.echo(json_bytes(summary).decode("utf-8"), nl=False)
        if not summary["all_observed_checks_match"] or not result["runs"]:
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("contract-examples")
def contract_examples(
    out: Annotated[Path, typer.Option(help="Fresh reference example directory.")],
    case: Annotated[str, typer.Option(help="order-lookup or support-triage")] = "order-lookup",
) -> None:
    """Write team-authored reference files; does not run analysis, planning or Docker."""
    from flowdelta.contract_examples import write_contract_examples

    try:
        if case == "order-lookup":
            write_contract_examples(Path.cwd(), out)
        elif case == "support-triage":
            from flowdelta.additional_examples import write_support_examples

            write_support_examples(Path.cwd(), out)
        else:
            raise InputError("REFERENCE_CASE_NOT_REGISTERED")
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


@app.command("plan")
def plan_command(
    bundle: Path,
    catalog: Annotated[Path, typer.Option(help="Registered template catalog JSON.")],
    cards: Annotated[Path, typer.Option(help="Local card directory.")],
    out: Annotated[Path, typer.Option(help="New plan file at the future bundle root.")],
) -> None:
    """Generate B's plan and sibling retrieval/KB files without running workflows."""
    from flowdelta.ingestion import _read_local, parse_file_model, parse_json
    from flowdelta.knowledge import load_cards, write_plan
    from flowdelta.pipeline_contracts import AnalysisBundle, TemplateCatalog
    from flowdelta.pipeline_io import read_handoff, read_manifest

    try:
        root = bundle.resolve()
        _, documents = read_manifest(root)
        if "analysis.json" not in documents:
            raise InputError("PIPELINE_FILE_MISSING")
        analysis = parse_file_model(AnalysisBundle, documents["analysis.json"])
        templates = parse_file_model(
            TemplateCatalog, parse_json(_read_local(catalog.parent.resolve(), catalog.name))
        )
        plan = write_plan(
            read_handoff(root / "input"),
            analysis,
            load_cards(cards, bundle_root=root),
            templates,
            out,
        )
        typer.echo(
            json_bytes(
                {
                    "status": "PLAN_WRITTEN",
                    "plan_id": plan.plan_id,
                    "analysis_origin": analysis.authorship.origin,
                    "execution_status": "NOT_RUN",
                }
            ).decode(),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("bundle")
def bundle_command(
    handoff: Path,
    out: Annotated[Path, typer.Option(help="New bundle directory; existing paths are rejected.")],
    artifact: Annotated[
        list[str] | None,
        typer.Option(help="Explicit bundle-relative-path=source-file mapping; repeat per file."),
    ] = None,
) -> None:
    """Preserve selected producer bytes and publish their manifest last."""
    from flowdelta.pipeline_bundle import write_module_bundle

    try:
        sources = {}
        for mapping in artifact or []:
            name, separator, source = mapping.partition("=")
            if not separator or not source:
                raise InputError("ARTIFACT_MAPPING_INVALID")
            if name in sources:
                raise InputError("ARTIFACT_PATH_COLLISION", location=name)
            sources[name] = Path(source)
        manifest = write_module_bundle(handoff, sources, out)
        typer.echo(
            json_bytes(
                {"status": "BUNDLE_WRITTEN", "artifact_count": len(manifest.artifacts)}
            ).decode(),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("execute")
def execute_command(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="New execution directory under var/.")],
) -> None:
    """Execute a registered plan and collect before/after runtime evidence."""
    from flowdelta.runtime.__main__ import main

    raise typer.Exit(code=main([str(bundle), "--out", str(out)]))


@app.command("demo-order-lookup")
def demo_order_lookup(
    out: Annotated[Path, typer.Option(help="New demo directory under var/.")],
) -> None:
    """Generate A/B outputs, execute C, bundle evidence and check the fixed normal case."""
    from flowdelta.demo import run_order_lookup_demo

    try:
        summary = run_order_lookup_demo(Path.cwd(), out)
        typer.echo(json_bytes(summary).decode(), nl=False)
        if not summary["all_normal_checks_passed"]:
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()
