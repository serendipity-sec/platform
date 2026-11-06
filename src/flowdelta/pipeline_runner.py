"""Explicit-input orchestration over existing A/B/C APIs; no case-specific oracle."""

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowdelta import __version__
from flowdelta.artifact_io import write_new_bytes
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
from flowdelta.pipeline_io import read_manifest
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.order_lookup import CommandRunner, subprocess_runner
from flowdelta.runtime.preflight import check_fresh_output, read_plan_bundle


def _code_hashes(repo: Path) -> dict[str, str]:
    root = Path(__file__).parent
    hashes = {
        f"src/flowdelta/{path.relative_to(root).as_posix()}": digest(path.read_bytes())
        for path in sorted(root.rglob("*.py"))
    }
    for path in sorted((repo / "runtime").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            hashes[path.relative_to(repo).as_posix()] = digest(path.read_bytes())
    for name in ("pyproject.toml", "uv.lock", "tools/check_profiled_order.py"):
        if (repo / name).is_file():
            hashes[name] = digest((repo / name).read_bytes())
    return hashes


def _save_summary(target: Path, summary: dict[str, Any]) -> None:
    write_new_bytes(target / "summary.json", json_bytes(summary))


def _allocated_runs(target: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((target / "execution/runs").glob("*/allocation.json")):
        try:
            value = parse_json(_read_local(path.parent, path.name))
            records.append({key: value[key] for key in ("run_id", "side", "attempt_number")})
        except (InputError, KeyError, OSError):
            continue
    return records


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
    runner: CommandRunner = subprocess_runner,
    sleep: Callable[[float], None] = time.sleep,
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
        stages[stage] = "RUNNING"
        summary["source_hashes"] = _code_hashes(repo)
        summary["source_tree_sha256"] = digest(json_bytes(summary["source_hashes"]))
        repository = FileInputRepository(target / "inputs")
        record = repository.save(case, profile, expected_profile_sha256=profile_sha256)
        handoff = repository.get(record)
        handoff_dir = repository.root / record.record_id / "handoff"
        (target / "input-record.json").write_bytes(json_bytes(record.model_dump(mode="json")))
        summary["input_record"] = record.model_dump(mode="json")
        summary["case_id"] = record.case_id
        stages[stage] = "SUCCEEDED"

        stage = "analysis"
        stages[stage] = "RUNNING"
        analysis = analyze(handoff)
        producers = target / "producers"
        producers.mkdir()
        analysis_path = producers / "analysis.json"
        analysis_path.write_bytes(json_bytes(analysis.model_dump(mode="json")))
        summary["analysis_id"] = analysis.analysis_id
        summary["analysis_origin"] = analysis.authorship.origin
        summary["analysis_limitations"] = analysis.limitations
        summary["analysis_graph_limits"] = analysis.extensions.get("flowdelta.graph.limits")
        stages[stage] = "SUCCEEDED"

        stage = "plan"
        stages[stage] = "RUNNING"
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
        plan_bundle = target / "plan-bundle"
        write_module_bundle(handoff_dir, sources, plan_bundle)
        validation = read_module_outputs(plan_bundle)
        if validation["runtime_record_coverage"] != "NOT_SUPPLIED":
            raise InputError("PIPELINE_UNEXPECTED_PRIOR_EXECUTION")
        frozen_manifest, frozen_docs = read_manifest(plan_bundle)
        frozen_manifest_hash = digest(_read_local(plan_bundle, "manifest.json"))
        # Final publication uses precisely the bundle consumed by C, not mutable producers.
        sources = {
            ref.path: plan_bundle / ref.path
            for ref in frozen_manifest.artifacts
            if not ref.path.startswith("input/")
        }
        handoff_dir = plan_bundle / "input"
        summary["retrieval_id"] = frozen_docs["retrieval.json"]["retrieval_id"]
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
            stages[stage] = "RUNNING"
            runtime_dir = target / "execution"
            checked_handoff, checked_plan, checked_catalog = read_plan_bundle(plan_bundle)
            runs = PlanExecutor(plan_bundle, repo, runner=runner, sleep=sleep).execute_plan(
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
                    "error_code": run.error_code,
                    "failed_phase": run.extensions.get("flowdelta.failed_phase"),
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
        stages[stage] = "RUNNING"
        current_manifest, _ = read_manifest(plan_bundle)
        if (
            current_manifest != frozen_manifest
            or digest(_read_local(plan_bundle, "manifest.json")) != frozen_manifest_hash
        ):
            raise InputError("PIPELINE_PLAN_BUNDLE_CHANGED")
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
        summary["failed_stage"] = stage
        summary["status"] = "FAILED"
        summary["error"] = exc.code if isinstance(exc, InputError) else "PIPELINE_IO_ERROR"
        if isinstance(exc, InputError):
            raise
        raise InputError("PIPELINE_IO_ERROR") from exc
    except Exception as exc:
        stages[stage] = "FAILED"
        summary["failed_stage"] = stage
        summary["status"] = "FAILED"
        summary["error"] = "PIPELINE_INTERNAL_ERROR"
        raise InputError("PIPELINE_INTERNAL_ERROR") from exc
    except BaseException:
        stages[stage] = "INTERRUPTED"
        summary.update(status="INTERRUPTED", failed_stage=stage, error="PIPELINE_INTERRUPTED")
        raise
    finally:
        original_error = sys.exception()
        try:
            summary["allocated_runs"] = _allocated_runs(target)
            _save_summary(target, summary)
        except (InputError, OSError):
            if original_error is not None:
                original_error.add_note("PIPELINE_SUMMARY_WRITE_FAILED")
            else:
                raise InputError("PIPELINE_SUMMARY_WRITE_FAILED") from None
    return summary
