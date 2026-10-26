"""Pure composition from a fixed retrieval result and canonical card snapshot."""

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError
from flowdelta.knowledge.cards import _KnowledgeSnapshot
from flowdelta.knowledge.retrieval import (
    author,
    context_fingerprint,
    exclusion_reason,
    hit_details,
    retrieval_content_fingerprint,
    retrieval_identity,
    search_context,
)
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
    return _compose_snapshot(
        handoff, analysis, _KnowledgeSnapshot.prepare(cards), catalog, retrieval, producer=producer
    )


def _compose_snapshot(
    handoff: Handoff,
    analysis: AnalysisBundle,
    snapshot: _KnowledgeSnapshot,
    catalog: TemplateCatalog,
    retrieval: RetrievalResult,
    *,
    producer: str,
) -> TestPlan:
    cards = snapshot.cards
    if retrieval.analysis_id != analysis.analysis_id:
        raise InputError("RETRIEVAL_ANALYSIS_MISMATCH")
    if (
        retrieval.query.get("rules_version") != producer
        or retrieval.authorship.producer != producer
    ):
        raise InputError("RETRIEVAL_VERSION_MISMATCH")
    if retrieval.query.get("context_fingerprint") != context_fingerprint(
        handoff, analysis, catalog, producer
    ):
        raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    profile, node_types, fields, tokens = search_context(handoff, analysis)
    expected_query = {
        "task_id": handoff.task.task_id,
        "runtime_profile": profile,
        "node_types": [str(value) for value in node_types],
        "changed_fields": [str(value) for value in fields],
        "rules_version": producer,
        "context_fingerprint": context_fingerprint(handoff, analysis, catalog, producer),
    }
    recorded_query = {
        key: value for key, value in retrieval.query.items() if key != "result_fingerprint"
    }
    if recorded_query != expected_query:
        raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    if retrieval.method != "KEYWORD" or retrieval.model_version is not None:
        raise InputError("RETRIEVAL_METHOD_MISMATCH")
    expected = snapshot.reference
    if (retrieval.kb_snapshot_ref.sha256, retrieval.kb_snapshot_ref.size_bytes) != (
        expected.sha256,
        expected.size_bytes,
    ):
        raise InputError("RETRIEVAL_SNAPSHOT_MISMATCH")
    if retrieval.retrieval_id != retrieval_identity(
        producer, snapshot.data, handoff, analysis, catalog
    ):
        raise InputError("RETRIEVAL_ID_MISMATCH")
    known = {(card.card_id, card.version) for card in cards}
    keys = [(hit.card_id, hit.card_version) for hit in retrieval.hits]
    if len(keys) != len(set(keys)) or not set(keys).issubset(known):
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    hit_keys = {f"{card_id}@{version}" for card_id, version in keys}
    card_keys = {f"{card_id}@{version}" for card_id, version in known}
    excluded_keys = set(retrieval.excluded_reasons)
    if (hit_keys & excluded_keys) or (hit_keys | excluded_keys) != card_keys:
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    if [hit.rank for hit in retrieval.hits] != list(range(1, len(keys) + 1)):
        raise InputError("RETRIEVAL_RANK_INVALID")
    expected_order = sorted(
        retrieval.hits,
        key=lambda hit: (-hit.score, hit.card_id, hit.card_version),
    )
    if retrieval.hits != expected_order:
        raise InputError("RETRIEVAL_RANK_INVALID")
    if any(hit.applicability == "NO_MATCH" for hit in retrieval.hits):
        raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    by_card = {(card.card_id, card.version): card for card in cards}
    node_type_set = set(node_types)
    templates = {
        (template.template_id, template.version): template for template in catalog.templates
    }
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        reason = exclusion_reason(card, handoff, templates, profile, node_type_set)
        if key in excluded_keys:
            if reason is None or reason != retrieval.excluded_reasons[key]:
                raise InputError("RETRIEVAL_CARD_MISMATCH")
        elif reason is not None:
            raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    for hit in retrieval.hits:
        card = by_card[(hit.card_id, hit.card_version)]
        score, applicability, reason = hit_details(card, node_type_set, tokens)
        if hit.applicability != applicability:
            raise InputError("RETRIEVAL_INELIGIBLE_CARD")
        if hit.score != score or hit.reason != reason:
            raise InputError("RETRIEVAL_CONTENT_MISMATCH")
    if retrieval.query.get("result_fingerprint") != retrieval_content_fingerprint(retrieval):
        raise InputError("RETRIEVAL_CONTENT_MISMATCH")
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
