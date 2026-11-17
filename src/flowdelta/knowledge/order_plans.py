"""B's bounded order test suite, composed from A and registered scenario definitions."""

from pathlib import Path

from pydantic import JsonValue

from flowdelta.contracts import ArtifactRef, Handoff
from flowdelta.ingestion import InputError, digest, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge.planner import write_plan
from flowdelta.order_scenarios import ROOT, SCENARIO_KEY, read_registered, validate_registration
from flowdelta.pipeline_contracts import AnalysisBundle, ScenarioCard, TemplateCatalog, TestPlan


def write_order_plans(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    *,
    repo: Path,
    output: Path,
) -> list[tuple[str, TestPlan, dict[str, Path]]]:
    """No pair labels or expected outcomes are available here. Normal input remains intact."""
    validate_registration(handoff, catalog, repo)
    if not any(rule.rule_id == "own-order" for rule in handoff.policy.rules):
        raise InputError("ORDER_POLICY_NOT_APPLICABLE")
    if not any(
        "orders-api:8080/" in effect
        for fact in handoff.semantics.semantics
        for effect in (fact.effects or [])
    ):
        raise InputError("ORDER_READ_PATH_NOT_FOUND")
    # One registered general business card; security trials are policy coverage obligations.
    card = parse_file_model(
        ScenarioCard,
        parse_json(read_registered(repo, f"{ROOT}/cards/business.json")),
    )
    base = write_plan(handoff, analysis, [card], catalog, output / "plan.json")
    path_ids = [path.path_id for path in analysis.impact_paths]
    reason: dict[str, JsonValue] = {
        "basis": "OWN_ORDER_POLICY_AND_REGISTERED_READ_PATH",
        "analysis_id": analysis.analysis_id,
        "impact_path_ids": list[JsonValue](path_ids),
        "priority": "CHANGED_PATH" if path_ids else "POLICY_BASELINE",
        "coverage": "NORMAL_OTHER_ORDER_AND_UNTRUSTED_IDENTITY",
    }
    base.planner_metadata["order_test_selection"] = reason
    results = []
    for scenario_id in ("normal", "other-order", "claimed-owner"):
        plan = base.model_copy(deep=True)
        sources = {name: output / name for name in ("retrieval.json", "kb-snapshot.json")}
        identity = {"base_plan": base.plan_id, "scenario_id": scenario_id}
        if scenario_id != "normal":
            data = read_registered(repo, f"{ROOT}/scenarios/{scenario_id}.json")
            scenario_file = output / f"{scenario_id}.json"
            scenario_file.write_bytes(data)
            plan.template_id = "order-evaluation-security"
            plan.extensions[SCENARIO_KEY] = ArtifactRef(
                artifact_id="order-scenario",
                path="scenario.json",
                sha256=digest(data),
                size_bytes=len(data),
                kind="source",
            ).model_dump(mode="json")
            sources["scenario.json"] = scenario_file
            identity["scenario_sha256"] = digest(data)
        plan.plan_id = "plan-" + digest(json_bytes(identity))[:24]
        plan.planner_metadata["scenario_id"] = scenario_id
        plan_path = output / f"{scenario_id}.plan.json"
        plan_path.write_bytes(json_bytes(plan.model_dump(mode="json")))
        sources["plan.json"] = plan_path
        results.append((scenario_id, plan, sources))
    return results
