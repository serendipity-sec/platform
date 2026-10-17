"""Pure composition from a fixed retrieval result and canonical card snapshot."""

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, digest
from flowdelta.knowledge.cards import _unique, kb_bytes
from flowdelta.knowledge.retrieval import author, context_fingerprint
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ObligationBinding,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import validate_input_fields, validate_pipeline


def compose_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    retrieval: RetrievalResult,
    *,
    producer: str,
) -> TestPlan:
    cards = _unique(cards)
    if retrieval.analysis_id != analysis.analysis_id:
        raise InputError("RETRIEVAL_ANALYSIS_MISMATCH")
    if retrieval.query.get("context_fingerprint") != context_fingerprint(
        handoff, analysis, catalog
    ):
        raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    snapshot = kb_bytes(cards)
    if (retrieval.kb_snapshot_ref.sha256, retrieval.kb_snapshot_ref.size_bytes) != (
        digest(snapshot),
        len(snapshot),
    ):
        raise InputError("RETRIEVAL_SNAPSHOT_MISMATCH")
    known = {(card.card_id, card.version) for card in cards}
    keys = [(hit.card_id, hit.card_version) for hit in retrieval.hits]
    if len(keys) != len(set(keys)) or not set(keys).issubset(known):
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    if [hit.rank for hit in retrieval.hits] != list(range(1, len(keys) + 1)):
        raise InputError("RETRIEVAL_RANK_INVALID")
    if any(hit.applicability == "NO_MATCH" for hit in retrieval.hits):
        raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    by_card = {(card.card_id, card.version): card for card in cards}
    profile = handoff.environment.extensions.get("flowdelta.semantic_profile")
    eligible = [
        t
        for t in catalog.templates
        if t.task_id == handoff.task.task_id and t.runtime_profile == profile
    ]
    if retrieval.hits:
        card = by_card[(retrieval.hits[0].card_id, retrieval.hits[0].card_version)]
        eligible = [
            t
            for t in eligible
            if (t.template_id, t.version) == (card.template_id, card.template_version)
        ]
    if not eligible:
        raise InputError("TEMPLATE_NOT_FOUND")
    template = None
    for candidate in sorted(eligible, key=lambda t: (t.template_id, t.version)):
        try:
            validate_input_fields(handoff.task.normal_input, candidate.input_fields)
        except InputError:
            continue
        template = candidate
        break
    if template is None:
        raise InputError("TEMPLATE_INPUT_INVALID")
    bindings = []
    uncovered = []
    for obligation in sorted(handoff.case.obligations, key=lambda item: item.obligation_id):
        if set(obligation.required_artifacts).issubset(template.required_artifact_kinds):
            bindings.append(
                ObligationBinding(
                    obligation_id=obligation.obligation_id,
                    required_artifact_kinds=sorted(obligation.required_artifacts),
                )
            )
        else:
            uncovered.append(obligation.obligation_id)
    state = next(
        ref
        for ref in handoff.source_manifest.artifacts
        if ref.path == handoff.case.initial_state_ref
    )
    plan = TestPlan(
        plan_id=f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}",
        case_id=handoff.case.case_id,
        analysis_id=analysis.analysis_id,
        authorship=author(analysis, producer),
        before_snapshot_id=analysis.before_snapshot_id,
        after_snapshot_id=analysis.after_snapshot_id,
        catalog_id=catalog.catalog_id,
        template_id=template.template_id,
        template_version=template.version,
        normal_input=handoff.task.normal_input,
        initial_state_ref=state.model_copy(update={"path": f"input/{state.path}"}),
        obligation_bindings=bindings,
        uncovered_obligation_ids=uncovered,
        attempts_per_side=1,
        timeout_seconds=template.timeout_seconds,
        planner_metadata={
            "rules_version": producer,
            "retrieval_empty": not retrieval.hits,
            "selection": "ranked-card" if retrieval.hits else "registered-normal-fallback",
            "security_outcome": None,
        },
    )
    validate_pipeline(handoff, analysis, catalog, plan)
    return plan
