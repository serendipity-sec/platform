"""B's bounded order test suite, composed from A and registered scenario definitions."""

from pathlib import Path

from pydantic import JsonValue

from flowdelta.contracts import ArtifactRef, Handoff
from flowdelta.ingestion import InputError, digest, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge.generation import (
    GenerationResult,
    HttpPlanningProvider,
    PlanningProvider,
    RecordedPlanningProvider,
)
from flowdelta.knowledge.plan_identity import expected_plan_id
from flowdelta.knowledge.planner import write_plan
from flowdelta.knowledge.planning_config import PlanningConfig
from flowdelta.knowledge.selection import TrialCandidate, select_trials
from flowdelta.order_scenarios import ROOT, SCENARIO_KEY, read_registered, validate_registration
from flowdelta.pipeline_contracts import AnalysisBundle, ScenarioCard, TemplateCatalog, TestPlan


def write_order_plans(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    *,
    repo: Path,
    output: Path,
    planning_config: PlanningConfig | None = None,
    provider: PlanningProvider | None = None,
    replay: GenerationResult | None = None,
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
    config = planning_config or PlanningConfig()
    cards = [card]
    if config.generation is not None:
        for name in ("claimed-customer", "claimed-tenant"):
            cards.append(
                parse_file_model(
                    ScenarioCard, parse_json(read_registered(repo, f"{ROOT}/cards/{name}.json"))
                )
            )
    if config.retrieval == "VECTOR":
        from flowdelta.knowledge.database import engine_for
        from flowdelta.knowledge.embedding_settings import EmbeddingSettings
        from flowdelta.knowledge.planner import write_vector_plan
        from flowdelta.knowledge.settings import KnowledgeSettings

        engine = engine_for(KnowledgeSettings.from_environment())
        try:
            base = write_vector_plan(
                handoff,
                analysis,
                catalog,
                output / "plan.json",
                engine=engine,
                provider=EmbeddingSettings.from_environment().provider(),
                limit=config.search_limit,
                pinned_cards={(c.card_id, c.version): c.sha256 for c in config.frozen_cards},
                expected_profile_id=config.embedding_profile_id,
            )
        finally:
            engine.dispose()
        snapshot = parse_json((output / "kb-snapshot.json").read_bytes())
        entries = snapshot.get("cards")
        if not isinstance(entries, list):
            raise InputError("KNOWLEDGE_OUTPUT_INVALID")
        cards = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("card"), dict):
                raise InputError("KNOWLEDGE_OUTPUT_INVALID")
            cards.append(ScenarioCard.model_validate(entry["card"]))
    else:
        base = write_plan(handoff, analysis, cards, catalog, output / "plan.json")
    selection = None
    scenario_ids = ["normal", "other-order", "claimed-owner"]
    if config.generation is not None:
        from flowdelta.pipeline_contracts import RetrievalResult

        candidates = registered_candidates(handoff, repo)
        retrieval = parse_file_model(
            RetrievalResult, parse_json((output / "retrieval.json").read_bytes())
        )
        selection = select_trials(
            handoff,
            analysis,
            catalog,
            retrieval,
            cards,
            candidates,
            config,
            provider=RecordedPlanningProvider(
                provider or HttpPlanningProvider(config.generation), output / "generation-call.json"
            ),
            replay=replay,
        )
        scenario_ids = selection.selected_candidate_ids
        (output / "selection.json").write_bytes(json_bytes(selection.model_dump(mode="json")))
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
    for scenario_id in scenario_ids:
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
        if selection is not None:
            plan.planner_metadata["plan_identity"] = "content-v1"
            plan.planner_metadata["selection"] = selection.model_dump(mode="json")
            if scenario_id != "normal":
                # Persist the small registered request with the content-bound plan so
                # existing exact-ID storage can export C's original input bytes.
                plan.planner_metadata["registered_scenario_utf8"] = data.decode("utf-8")
            plan.plan_id = (
                expected_plan_id(plan, base.retrieval_ref.artifact_id) if base.retrieval_ref else ""
            )
        plan_path = output / f"{scenario_id}.plan.json"
        plan_path.write_bytes(json_bytes(plan.model_dump(mode="json")))
        sources["plan.json"] = plan_path
        results.append((scenario_id, plan, sources))
    return results


def registered_candidates(handoff: Handoff, repo: Path) -> list[TrialCandidate]:
    document = parse_json(read_registered(repo, f"{ROOT}/selection-catalog.json"))
    items = document.get("candidates")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise InputError("SELECTION_CATALOG_INVALID")
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            raise InputError("SELECTION_CATALOG_INVALID")
        values = dict(item)
        scenario_id = values["candidate_id"]
        if scenario_id == "normal":
            values["request_input"] = handoff.task.normal_input
        else:
            from flowdelta.order_scenarios import OrderScenario

            data = read_registered(repo, f"{ROOT}/scenarios/{scenario_id}.json")
            scenario = parse_file_model(OrderScenario, parse_json(data))
            if scenario.scenario_id != scenario_id:
                raise InputError("ORDER_SCENARIO_NOT_REGISTERED")
            values["request_input"] = scenario.request_input
            values["scenario_ref"] = ArtifactRef(
                artifact_id="order-scenario",
                path="scenario.json",
                sha256=digest(data),
                size_bytes=len(data),
                kind="source",
            ).model_dump(mode="json")
        candidates.append(TrialCandidate.model_validate(values))
    return candidates
