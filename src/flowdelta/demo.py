"""Reproduce one team-defined normal case through the real A/B/C public interfaces."""

from pathlib import Path
from typing import Any

from flowdelta.graph import analyze
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case, parse_file_model, parse_json
from flowdelta.knowledge import load_cards, write_plan
from flowdelta.module_outputs import read_module_outputs
from flowdelta.order_verification import normal_checks
from flowdelta.pipeline_bundle import write_module_bundle
from flowdelta.pipeline_contracts import TemplateCatalog
from flowdelta.pipeline_io import read_handoff
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.preflight import check_fresh_output, read_plan_bundle


def run_order_lookup_demo(repo: Path, output: Path) -> dict[str, Any]:
    """No result substitution: generated analysis and plan are the executor's inputs."""
    repo = repo.resolve(strict=True)
    target = check_fresh_output(output, repo)
    target.mkdir(parents=True)
    loaded = load_case(repo / "examples/order-lookup")
    handoff_dir = target / "handoff"
    write_handoff(loaded, build_handoff(loaded), handoff_dir)
    handoff = read_handoff(handoff_dir)
    analysis = analyze(handoff)
    producers = target / "producers"
    producers.mkdir()
    analysis_path = producers / "analysis.json"
    analysis_path.write_bytes(json_bytes(analysis.model_dump(mode="json")))
    reference = repo / "examples/contracts/order-lookup"
    catalog_path = reference / "template-catalog.json"
    catalog = parse_file_model(TemplateCatalog, parse_json(catalog_path.read_bytes()))
    sources = {"analysis.json": analysis_path, "template-catalog.json": catalog_path}
    # B's external source references, if any, resolve against an explicit analysis bundle.
    analysis_bundle = target / "analysis-bundle"
    write_module_bundle(handoff_dir, sources, analysis_bundle)
    cards = load_cards(reference / "cards", bundle_root=analysis_bundle)
    plan = write_plan(handoff, analysis, cards, catalog, producers / "plan.json")
    for name in ("plan.json", "retrieval.json", "kb-snapshot.json"):
        sources[name] = producers / name
    plan_bundle = target / "plan-bundle"
    write_module_bundle(handoff_dir, sources, plan_bundle)
    before_execution = read_module_outputs(plan_bundle)
    if before_execution["runtime_record_coverage"] != "NOT_SUPPLIED":
        raise InputError("DEMO_UNEXPECTED_PRIOR_EXECUTION")

    runtime_dir = target / "execution"
    handoff, plan, catalog = read_plan_bundle(plan_bundle)
    runs = PlanExecutor(plan_bundle, repo).execute_plan(handoff, plan, catalog, runtime_dir)
    for run in runs:
        record = f"runs/{run.side}-{run.attempt_number}.run.json"
        sources[record] = runtime_dir / record
        for ref in run.artifacts.artifacts:
            sources[ref.path] = runtime_dir / ref.path
    bundle = target / "bundle"
    manifest = write_module_bundle(handoff_dir, sources, bundle)
    validation = read_module_outputs(bundle)

    # This expectation predates the generated plan and is a team-defined normal oracle.
    # It checks this case's observable business behavior, not security detection quality.
    expected = parse_json((repo / "runtime/order-lookup/expected.json").read_bytes())
    observed: list[dict[str, Any]] = []
    for run in runs:
        root = bundle / f"runs/{run.side}-{run.attempt_number}"

        def document(name: str, root: Path = root) -> dict[str, Any]:
            path = root / name
            return parse_json(path.read_bytes()) if path.is_file() else {}

        response = document("response.json")
        initial, final = document("initial-state.json"), document("final-state.json")
        checks = normal_checks(
            run, expected, plan.normal_input, response, initial, final, document("events.json")
        )
        observed.append(
            {"side": run.side, "run_id": run.run_id, "response": response, "checks": checks}
        )
    summary = {
        "schema_version": "0.4.0",
        "case_id": handoff.case.case_id,
        "analysis_id": analysis.analysis_id,
        "analysis_origin": analysis.authorship.origin,
        "change_count": len(analysis.changes),
        "plan_id": plan.plan_id,
        "plan_origin": plan.authorship.origin,
        "comparison_run_id": runs[0].comparison_run_id,
        "bundle": "bundle",
        "artifact_count": len(manifest.artifacts),
        "contract_validation": validation,
        "normal_expectation_origin": expected["origin"],
        "all_normal_checks_passed": all(all(item["checks"].values()) for item in observed),
        "observations": observed,
        "security_outcome": None,
    }
    (target / "summary.json").write_bytes(json_bytes(summary))
    return summary
