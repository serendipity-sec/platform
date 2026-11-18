"""Consumer checks for files produced by A/B/C; never labels reference examples as their code."""

from pathlib import Path
from typing import Any

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError
from flowdelta.ingestion import parse_file_model as parse_model
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ExecutionRun,
    RetrievalResult,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import read_handoff, read_manifest, validate_pipeline, verify_ref


def validate_runs(
    handoff: Handoff,
    catalog: TemplateCatalog,
    plan: TestPlan,
    runs: list[ExecutionRun],
    root: Path,
    *,
    require_declared_run_artifacts: bool = True,
) -> None:
    expected = {
        (side, attempt)
        for side in ("before", "after")
        for attempt in range(1, plan.attempts_per_side + 1)
    }
    if {(run.side, run.attempt_number) for run in runs} != expected or len(runs) != len(expected):
        raise InputError("RUN_ATTEMPT_COVERAGE_MISMATCH")
    if len({run.run_id for run in runs}) != len(runs):
        raise InputError("RUN_ID_REUSED")
    if len({run.comparison_run_id for run in runs}) != 1:
        raise InputError("COMPARISON_RUN_ID_MISMATCH")
    template = next(
        item
        for item in catalog.templates
        if (item.template_id, item.version) == (plan.template_id, plan.template_version)
    )
    for run in runs:
        snapshot_id = plan.before_snapshot_id if run.side == "before" else plan.after_snapshot_id
        if (run.case_id, run.plan_id, run.snapshot_id, run.runtime_profile) != (
            handoff.case.case_id,
            plan.plan_id,
            snapshot_id,
            template.runtime_profile,
        ):
            raise InputError("RUN_CONTEXT_MISMATCH")
        if run.initial_state_sha256 != plan.initial_state_ref.sha256:
            raise InputError("STATE_HASH_MISMATCH")
        if require_declared_run_artifacts and not set(template.required_artifact_kinds).issubset(
            run.artifacts.required_kinds
        ):
            raise InputError("RUN_REQUIRED_ARTIFACT_MISSING")
        if (
            run.execution_status == "SUCCEEDED"
            and run.actual_engine_version != handoff.environment.target_version
        ):
            raise InputError("RUN_ENGINE_VERSION_MISMATCH")
        for ref in run.artifacts.artifacts:
            verify_ref(root, ref)


def read_module_outputs(
    root: Path, *, require_declared_run_artifacts: bool = True
) -> dict[str, Any]:
    """Validate module outputs, optionally leaving missing kind declarations to the judge."""
    _, docs = read_manifest(root)
    handoff = read_handoff(root / "input")
    try:
        analysis = parse_model(AnalysisBundle, docs["analysis.json"])
        catalog = parse_model(TemplateCatalog, docs["template-catalog.json"])
        plan = parse_model(TestPlan, docs["plan.json"])
    except KeyError as exc:
        raise InputError("PIPELINE_FILE_MISSING") from exc
    for name in ["analysis.json", "template-catalog.json", "plan.json"]:
        if docs[name].get("schema_version") != "0.4.0":
            raise InputError("PIPELINE_SCHEMA_VERSION_MISMATCH", location=f"{name}#/schema_version")
    if analysis.authorship.origin != "MODULE_OUTPUT" or plan.authorship.origin != "MODULE_OUTPUT":
        raise InputError("MODULE_OUTPUT_REQUIRED")
    validate_pipeline(handoff, analysis, catalog, plan)
    from flowdelta.order_scenarios import request_input, scenario_ref

    if scenario_ref(plan) is not None:
        from flowdelta.pipeline_io import validate_input_fields

        template = next(
            t
            for t in catalog.templates
            if t.template_id == plan.template_id and t.version == plan.template_version
        )
        validate_input_fields(request_input(plan, root), template.input_fields)
    if plan.retrieval_ref is not None:
        verify_ref(root, plan.retrieval_ref)
        if plan.retrieval_ref.path not in docs:
            raise InputError("RETRIEVAL_NOT_IN_MANIFEST")
        retrieval = parse_model(RetrievalResult, docs[plan.retrieval_ref.path])
        if (
            retrieval.analysis_id != analysis.analysis_id
            or retrieval.authorship.origin != "MODULE_OUTPUT"
        ):
            raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
        verify_ref(root, retrieval.kb_snapshot_ref)
    runs = [
        parse_model(ExecutionRun, doc)
        for name, doc in docs.items()
        if name.startswith("runs/") and name.endswith(".run.json")
    ]
    for run in runs:
        if run.authorship.origin != "RUNTIME_OBSERVATION":
            raise InputError("RUNTIME_OBSERVATION_REQUIRED")
    if runs:
        validate_runs(
            handoff,
            catalog,
            plan,
            runs,
            root,
            require_declared_run_artifacts=require_declared_run_artifacts,
        )
    return {
        "schema_version": "0.4.0",
        "status": "CONTRACT_LINKS_VALID",
        "case_id": handoff.case.case_id,
        "analysis_producer": analysis.authorship.producer,
        "plan_producer": plan.authorship.producer,
        "run_count": len(runs),
        "runtime_record_coverage": "CHECKED" if runs else "NOT_SUPPLIED",
        "execution_states": [run.execution_status for run in runs],
        "missing_artifact_kinds": sorted(
            {kind for run in runs for kind in run.artifacts.missing_kinds}
        ),
        "outcome": None,
    }
