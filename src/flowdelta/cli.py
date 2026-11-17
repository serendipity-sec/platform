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
from flowdelta.runtime.static_db_cli import app as static_db_app
from flowdelta.runtime.static_report import summarize_command

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(analysis_db_app, name="analysis-db")
app.add_typer(static_db_app, name="static-db")
app.command("static-report")(summarize_command)


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


@app.command("freeze-profile")
def freeze_profile_command(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="New profile JSON; parent must exist.")],
    profile_id: Annotated[str, typer.Option()],
    revision: Annotated[str, typer.Option()],
    fixed_node: Annotated[list[str] | None, typer.Option(help="Additional fixed node ID.")] = None,
) -> None:
    """Explicitly establish a workflow-only baseline and print its canonical digest."""
    from flowdelta.fixed_profile import freeze_profile, profile_digest, write_profile

    try:
        loaded = load_case(bundle)
        profile = freeze_profile(
            loaded,
            profile_id=profile_id,
            revision=revision,
            additional_fixed_node_ids=tuple(fixed_node or []),
        )
        write_profile(profile, out, input_root=loaded.root)
        typer.echo(json_bytes({"profile_sha256": profile_digest(profile)}).decode(), nl=False)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("check-profile")
def check_profile_command(
    bundle: Path,
    profile: Annotated[Path, typer.Option()],
    profile_sha256: Annotated[str, typer.Option(help="Trusted canonical profile digest.")],
) -> None:
    """Check fixed inputs without running the workflow or accepting a new baseline."""
    from flowdelta.fixed_profile import read_profile, validate_profile

    try:
        pinned = read_profile(profile, expected_sha256=profile_sha256)
        result = validate_profile(load_case(bundle), pinned)
        typer.echo(json_bytes(result.model_dump(mode="json")).decode(), nl=False)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("profile-snapshot")
def profile_snapshot_command(
    bundle: Path,
    profile: Annotated[Path, typer.Option()],
    profile_sha256: Annotated[str, typer.Option(help="Trusted canonical profile digest.")],
    out: Annotated[Path, typer.Option(help="New output directory; parent must exist.")],
) -> None:
    """Gate captured source bytes, then write an ordinary 0.4.0 handoff for A/B/C."""
    from flowdelta.fixed_profile import load_profiled_handoff

    try:
        loaded, handoff, result = load_profiled_handoff(
            bundle, profile, expected_sha256=profile_sha256
        )
        write_handoff(loaded, handoff, out)
        typer.echo(json_bytes(result.model_dump(mode="json")).decode(), nl=False)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
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
    out: Annotated[Path, typer.Option(help="New plan file at the future bundle root.")],
    cards: Annotated[Path | None, typer.Option(help="Local card directory.")] = None,
    selected_path_id: Annotated[
        str | None, typer.Option("--path-id", help="Explicit A impact path ID for static planning.")
    ] = None,
    persist: Annotated[
        bool,
        typer.Option(
            help="Store local-card output in PostgreSQL; shared-card output is always stored."
        ),
    ] = False,
    shared_cards: Annotated[
        bool,
        typer.Option(
            help="Use shared vector retrieval and always persist exact B outputs to PostgreSQL."
        ),
    ] = False,
) -> None:
    """Generate a normal or static plan; shared-vector output is always stored."""
    from flowdelta.ingestion import _read_local, parse_file_model, parse_json
    from flowdelta.knowledge import load_cards, write_plan
    from flowdelta.knowledge.planner import write_vector_plan
    from flowdelta.pipeline_contracts import AnalysisBundle, TemplateCatalog
    from flowdelta.pipeline_io import read_handoff, read_manifest
    from flowdelta.runtime.static_scan import StaticScanCatalog

    engine = None
    try:
        if shared_cards and cards is not None:
            raise InputError("PLAN_CARD_SOURCE_CONFLICT")
        if not shared_cards and cards is None:
            raise InputError("PLAN_CARD_SOURCE_REQUIRED")

        root = bundle.resolve()
        _, documents = read_manifest(root)
        if "analysis.json" not in documents:
            raise InputError("PIPELINE_FILE_MISSING")
        analysis = parse_file_model(AnalysisBundle, documents["analysis.json"])
        catalog_data = parse_json(_read_local(catalog.parent.resolve(), catalog.name))
        entries = catalog_data.get("templates")
        is_static_catalog = (
            isinstance(entries, list)
            and bool(entries)
            and isinstance(entries[0], dict)
            and entries[0].get("mode") == "STATIC_SCAN"
        )
        templates: TemplateCatalog | StaticScanCatalog
        if is_static_catalog:
            templates = parse_file_model(StaticScanCatalog, catalog_data)
        else:
            templates = parse_file_model(TemplateCatalog, catalog_data)
        if shared_cards or persist:
            from flowdelta.knowledge.database import engine_for
            from flowdelta.knowledge.settings import KnowledgeSettings

            engine = engine_for(KnowledgeSettings.from_environment())

        if shared_cards:
            from flowdelta.knowledge.embedding_settings import EmbeddingSettings

            assert engine is not None
            plan = write_vector_plan(
                read_handoff(root / "input"),
                analysis,
                templates,
                out,
                engine=engine,
                provider=EmbeddingSettings.from_environment().provider(),
                handoff_root=root / "input",
                repo_root=Path(__file__).resolve().parents[2],
                selected_path_id=selected_path_id,
            )
        else:
            assert cards is not None
            plan = write_plan(
                read_handoff(root / "input"),
                analysis,
                load_cards(cards, bundle_root=root),
                templates,
                out,
                handoff_root=root / "input",
                repo_root=Path(__file__).resolve().parents[2],
                selected_path_id=selected_path_id,
            )
        if engine is not None:
            from flowdelta.knowledge.repository import store_b_outputs

            store_b_outputs(engine, out)
        database_status = "STORED" if engine is not None else "NOT_REQUESTED"
        typer.echo(
            json_bytes(
                {
                    "status": "PLAN_WRITTEN",
                    "plan_id": plan.plan_id,
                    "retrieval_id": (
                        plan.retrieval_ref.artifact_id if plan.retrieval_ref is not None else None
                    ),
                    "selected_path_id": plan.planner_metadata.get("selected_path_id"),
                    "analysis_origin": analysis.authorship.origin,
                    "database_status": database_status,
                    "execution_status": "NOT_RUN",
                }
            ).decode(),
            nl=False,
        )
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None
    except ModuleNotFoundError as exc:
        if exc.name not in {"alembic", "psycopg", "sqlalchemy"}:
            raise
        typer.echo(
            json_bytes({"error": "KNOWLEDGE_EXTRA_REQUIRED"}).decode("utf-8"),
            err=True,
            nl=False,
        )
        raise typer.Exit(code=2) from None
    finally:
        if engine is not None:
            engine.dispose()


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
    judge_order: Annotated[
        bool, typer.Option(help="Also judge registered own-order read observations.")
    ] = False,
) -> None:
    """Generate A/B outputs, execute C, bundle evidence and check the fixed normal case."""
    from flowdelta.demo import run_order_lookup_demo

    try:
        summary = run_order_lookup_demo(Path.cwd(), out, judge_order=judge_order)
        typer.echo(json_bytes(summary).decode(), nl=False)
        if (
            not summary["all_normal_checks_passed"]
            or summary.get("judgment", {}).get("status", "COMPLETE") != "COMPLETE"
        ):
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("run-pipeline")
def run_pipeline_command(
    case: Path,
    profile: Annotated[Path, typer.Option()],
    profile_sha256: Annotated[str, typer.Option()],
    catalog: Annotated[Path, typer.Option()],
    cards: Annotated[Path, typer.Option()],
    out: Annotated[Path, typer.Option(help="New output directory under var/.")],
    execute: Annotated[bool, typer.Option(help="Execute only C-registered templates.")] = False,
    judge_order: Annotated[
        bool, typer.Option(help="Judge registered own-order reads; requires --execute.")
    ] = False,
) -> None:
    """Profile gate → stored input → A → B → optional C → verified artifact bundle."""
    from flowdelta.pipeline_runner import run_pipeline

    try:
        result = run_pipeline(
            repo=Path.cwd(),
            case=case,
            profile=profile,
            profile_sha256=profile_sha256,
            catalog=catalog,
            cards=cards,
            output=out,
            execute=execute,
            judge_order=judge_order,
        )
        typer.echo(json_bytes(result).decode(), nl=False)
        if result["status"] == "INCOMPLETE":
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("evaluate-order-family")
def evaluate_order_family_command(
    manifest: Annotated[Path, typer.Option(help="Registered order family manifest.")],
    out: Annotated[Path, typer.Option(help="New output directory under var/.")],
    execute: Annotated[
        bool, typer.Option(help="Execute selected plans and judge independent evidence.")
    ] = False,
) -> None:
    """A analysis → B registered scenarios → optional C execution and automatic judgment."""
    from flowdelta.order_evaluation import evaluate_order_family

    try:
        result = evaluate_order_family(
            repo=Path.cwd(), manifest=manifest, output=out, execute=execute
        )
        typer.echo(
            json_bytes(
                {"status": result["status"], "pairs": len(result["pairs"]), "output": str(out)}
            ).decode(),
            nl=False,
        )
        if result["status"] == "INCOMPLETE":
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("judge-order-evaluation-bundle")
def judge_order_evaluation_bundle_command(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="New judgment directory under var/.")],
) -> None:
    """Rejudge a registered evaluation bundle without running Docker."""
    from flowdelta.order_evaluation import judge_evaluation_bundle
    from flowdelta.runtime.preflight import check_fresh_output

    try:
        target = check_fresh_output(out, Path.cwd())
        report = judge_evaluation_bundle(bundle, Path.cwd())
        target.mkdir(parents=True, exist_ok=False)
        (target / "report.json").write_bytes(json_bytes(report))
        typer.echo(json_bytes({"status": report["status"], "output": str(target)}).decode())
        if report["status"] == "INCOMPLETE":
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


@app.command("judge-order-bundle")
def judge_order_bundle_command(
    bundle: Path,
    out: Annotated[Path, typer.Option(help="New judgment directory under var/.")],
) -> None:
    """Judge existing registered order evidence without executing workflows again."""
    from flowdelta.order_judgment import publish_order_judgments
    from flowdelta.runtime.preflight import check_fresh_output

    try:
        repo = Path.cwd()
        target = check_fresh_output(out, repo)
        result = publish_order_judgments(bundle, repo, target)
        typer.echo(json_bytes(result).decode(), nl=False)
        if result["status"] != "COMPLETE":
            raise typer.Exit(code=2)
    except InputError as exc:
        typer.echo(json_bytes(exc.diagnostic()).decode(), err=True, nl=False)
        raise typer.Exit(code=2) from None


if __name__ == "__main__":
    app()
