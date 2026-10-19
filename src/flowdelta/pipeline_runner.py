"""Explicit-input orchestration over existing A/B/C APIs; no case-specific oracle."""

from pathlib import Path
from typing import Any
from uuid import uuid4

from flowdelta import __version__
from flowdelta.graph import analyze
from flowdelta.ingestion import (
    InputError,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.input_repository import FileInputRepository
from flowdelta.knowledge import load_cards, write_plan
from flowdelta.module_outputs import read_module_outputs
from flowdelta.pipeline_bundle import write_module_bundle
from flowdelta.pipeline_contracts import TemplateCatalog
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.preflight import check_fresh_output, read_plan_bundle


def _code_hashes() -> dict[str, str]:
    root = Path(__file__).parent
    names = [
        "pipeline_runner.py",
        "input_repository.py",
        "fixed_profile.py",
        "handoff.py",
        "graph/analyze.py",
        "knowledge/planner.py",
        "runtime/executor.py",
        "pipeline_bundle.py",
    ]
    return {name: digest((root / name).read_bytes()) for name in names}


def run_pipeline(
    *,
    repo: Path,
    case: Path,
    profile: Path,
    profile_sha256: str,
    catalog: Path,
    cards: Path,
    output: Path,
    execute: bool = False,
) -> dict[str, Any]:
    """Prepare by default. Execution always delegates registration and hash checks to C."""
    repo = repo.resolve(strict=True)
    target = check_fresh_output(output, repo)
    for source in (case, profile, catalog, cards):
        resolved = source.resolve()
        if target.is_relative_to(resolved) or resolved.is_relative_to(target):
            raise InputError("OUTPUT_OVERLAPS_INPUT")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise InputError("OUTPUT_WRITE_FAILED") from exc
    stages = {key: "NOT_RUN" for key in ("input", "analysis", "plan", "execution", "bundle")}
    summary: dict[str, Any] = {
        "schema_version": "0.4.0",
        "record_format": "flowdelta-pipeline-run-v1",
        "orchestration_id": f"pipeline-{uuid4().hex}",
        "producer_version": __version__,
        "mode": "EXECUTE_REGISTERED" if execute else "PREPARE",
        "status": "IN_PROGRESS",
        "stages": stages,
        "security_outcome": None,
        "storage": "LOCAL_FILE_REPOSITORY",
    }
    stage = "input"
    try:
        summary["entrypoint_source_hashes"] = _code_hashes()
        repository = FileInputRepository(target / "inputs")
        record = repository.save(case, profile, expected_profile_sha256=profile_sha256)
        handoff = repository.get(record)
        handoff_dir = repository.root / record.record_id / "handoff"
        (target / "input-record.json").write_bytes(json_bytes(record.model_dump(mode="json")))
        summary["input_record"] = record.model_dump(mode="json")
        summary["case_id"] = record.case_id
        stages[stage] = "SUCCEEDED"

        stage = "analysis"
        analysis = analyze(handoff)
        producers = target / "producers"
        producers.mkdir()
        analysis_path = producers / "analysis.json"
        analysis_path.write_bytes(json_bytes(analysis.model_dump(mode="json")))
        summary["analysis_id"] = analysis.analysis_id
        summary["analysis_origin"] = analysis.authorship.origin
        summary["analysis_limitations"] = analysis.limitations
        stages[stage] = "SUCCEEDED"

        stage = "plan"
        # Capture the catalog once. The planner and bundle see these same bytes.
        catalog_data = _read_local(catalog.absolute().parent.resolve(strict=True), catalog.name)
        parsed_catalog = parse_file_model(TemplateCatalog, parse_json(catalog_data))
        catalog_path = producers / "template-catalog.json"
        catalog_path.write_bytes(catalog_data)
        sources = {"analysis.json": analysis_path, "template-catalog.json": catalog_path}
        analysis_bundle = target / "analysis-bundle"
        write_module_bundle(handoff_dir, sources, analysis_bundle)
        parsed_cards = load_cards(cards, bundle_root=analysis_bundle)
        plan = write_plan(handoff, analysis, parsed_cards, parsed_catalog, producers / "plan.json")
        for name in ("plan.json", "retrieval.json", "kb-snapshot.json"):
            sources[name] = producers / name
        summary["retrieval_id"] = parse_json((producers / "retrieval.json").read_bytes())[
            "retrieval_id"
        ]
        plan_bundle = target / "plan-bundle"
        write_module_bundle(handoff_dir, sources, plan_bundle)
        validation = read_module_outputs(plan_bundle)
        if validation["runtime_record_coverage"] != "NOT_SUPPLIED":
            raise InputError("PIPELINE_UNEXPECTED_PRIOR_EXECUTION")
        summary.update(
            plan_id=plan.plan_id,
            plan_origin=plan.authorship.origin,
            catalog_sha256=digest(catalog_data),
            retrieval_sha256=digest((producers / "retrieval.json").read_bytes()),
            kb_sha256=digest((producers / "kb-snapshot.json").read_bytes()),
        )
        stages[stage] = "SUCCEEDED"

        if execute:
            stage = "execution"
            runtime_dir = target / "execution"
            checked_handoff, checked_plan, checked_catalog = read_plan_bundle(plan_bundle)
            runs = PlanExecutor(plan_bundle, repo).execute_plan(
                checked_handoff, checked_plan, checked_catalog, runtime_dir
            )
            if not runs:
                raise InputError("PIPELINE_NO_EXECUTION_RECORDS")
            summary["runs"] = [
                {
                    "run_id": run.run_id,
                    "side": run.side,
                    "attempt_number": run.attempt_number,
                    "execution_status": run.execution_status,
                    "collection_status": run.artifacts.collection_status,
                    "cleanup_status": run.cleanup_status,
                }
                for run in runs
            ]
            for run in runs:
                name = f"runs/{run.side}-{run.attempt_number}.run.json"
                sources[name] = runtime_dir / name
                for ref in run.artifacts.artifacts:
                    sources[ref.path] = runtime_dir / ref.path
            stages[stage] = (
                "SUCCEEDED"
                if all(
                    run.execution_status == "SUCCEEDED"
                    and run.artifacts.collection_status == "COMPLETE"
                    and run.cleanup_status == "COMPLETE"
                    for run in runs
                )
                else "INCOMPLETE"
            )

        stage = "bundle"
        final_bundle = target / "bundle"
        manifest = write_module_bundle(handoff_dir, sources, final_bundle)
        summary["contract_validation"] = read_module_outputs(final_bundle)
        summary["bundle"] = "bundle"
        summary["bundle_manifest_sha256"] = digest(json_bytes(manifest.model_dump(mode="json")))
        stages[stage] = "SUCCEEDED"
        summary["status"] = (
            "INCOMPLETE"
            if "INCOMPLETE" in stages.values()
            else ("EXECUTED" if execute else "PREPARED")
        )
    except (InputError, OSError) as exc:
        stages[stage] = "FAILED"
        summary["status"] = "FAILED"
        summary["error"] = exc.code if isinstance(exc, InputError) else "PIPELINE_IO_ERROR"
        if isinstance(exc, InputError):
            raise
        raise InputError("PIPELINE_IO_ERROR") from exc
    except Exception as exc:
        stages[stage] = "FAILED"
        summary["status"] = "FAILED"
        summary["error"] = "PIPELINE_INTERNAL_ERROR"
        raise InputError("PIPELINE_INTERNAL_ERROR") from exc
    finally:
        (target / "summary.json").write_bytes(json_bytes(summary))
    return summary
