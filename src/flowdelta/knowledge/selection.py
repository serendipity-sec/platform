"""Validate model suggestions and reserve mandatory coverage before spending trial budget."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from flowdelta.contracts import ArtifactRef, Contract, Handoff, Identifier, NonEmpty
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.generation import GenerationResult, PlanningProvider, Proposal
from flowdelta.knowledge.planning_config import PlanningConfig
from flowdelta.knowledge.retrieval import vector_query_context
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
)
from flowdelta.pipeline_io import validate_input_fields


class TrialCandidate(Contract):
    candidate_id: Identifier
    description: NonEmpty
    template_id: Identifier
    template_version: NonEmpty
    mandatory: bool
    policy_rule_ids: list[Identifier]
    node_types: list[NonEmpty]
    card_keys: list[NonEmpty]
    required_artifact_kinds: list[Identifier]
    request_input: dict[str, JsonValue]
    scenario_ref: ArtifactRef | None = None


class CandidateDecision(Contract):
    candidate_id: Identifier
    selected: bool
    mandatory: bool
    rank: int | None
    reason: NonEmpty
    card_keys: list[str] = Field(default_factory=list)
    path_ids: list[str] = Field(default_factory=list)


class PlanSelection(Contract):
    selection_format: Literal["flowdelta-plan-selection-v1"] = "flowdelta-plan-selection-v1"
    selection_id: str
    analysis_id: str
    retrieval_id: str
    config_sha256: str
    retrieval_sha256: str
    kb_sha256: str
    catalog_sha256: str
    candidate_catalog_sha256: str
    search_status: Literal["HITS", "EMPTY_RETRIEVAL"]
    generation: dict[str, JsonValue]
    proposal: Proposal
    decisions: Annotated[list[CandidateDecision], Field(max_length=100)]
    selected_candidate_ids: list[str]
    reserved_c_runs: int


def select_trials(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    retrieval: RetrievalResult,
    cards: list[ScenarioCard],
    candidates: list[TrialCandidate],
    config: PlanningConfig,
    *,
    provider: PlanningProvider | None,
    replay: GenerationResult | None = None,
) -> PlanSelection:
    """No evaluation truth is accepted. An invalid proposal fails the whole selection."""
    if retrieval.analysis_id != analysis.analysis_id or analysis.case_id != handoff.case.case_id:
        raise InputError("SELECTION_ANALYSIS_MISMATCH")
    from flowdelta.knowledge.planner import compose_plan

    base = compose_plan(handoff, analysis, cards, catalog, retrieval)
    if retrieval.method != config.retrieval:
        raise InputError("SELECTION_RETRIEVAL_METHOD_MISMATCH")
    required = {o.obligation_id for o in handoff.case.obligations if o.required}
    if required.intersection(base.uncovered_obligation_ids):
        raise InputError("MANDATORY_OBLIGATION_UNCOVERED")
    if (
        not candidates
        or not any(c.mandatory for c in candidates)
        or len(candidates) > 100
        or len({c.candidate_id for c in candidates}) != len(candidates)
    ):
        raise InputError("SELECTION_CANDIDATE_DUPLICATE")
    policy_ids = {rule.rule_id for rule in handoff.policy.rules}
    node_types = {n.node_type for w in (handoff.before, handoff.after) for n in w.nodes}
    templates = {(t.template_id, t.version): t for t in catalog.templates}
    hits = {f"{hit.card_id}@{hit.card_version}": hit for hit in retrieval.hits}
    eligible = {}
    decisions = []
    for candidate in candidates:
        template = templates.get((candidate.template_id, candidate.template_version))
        reason = None
        if template is None:
            reason = "TEMPLATE_NOT_REGISTERED"
        elif (
            template.task_id != handoff.task.task_id
            or template.runtime_profile
            != handoff.environment.extensions.get("flowdelta.semantic_profile")
        ):
            reason = "TEMPLATE_CONTEXT_MISMATCH"
        elif not set(candidate.policy_rule_ids).issubset(policy_ids):
            reason = "POLICY_NOT_APPLICABLE"
        elif candidate.node_types and not set(candidate.node_types).intersection(node_types):
            reason = "NODE_TYPE_MISMATCH"
        elif not set(candidate.required_artifact_kinds).issubset(template.required_artifact_kinds):
            reason = "REQUIRED_EVIDENCE_UNSUPPORTED"
        else:
            validate_input_fields(candidate.request_input, template.input_fields)
            if not candidate.mandatory and not set(candidate.card_keys).intersection(hits):
                reason = "NO_RETRIEVED_EVIDENCE"
        if reason and candidate.mandatory:
            raise InputError("MANDATORY_TEST_UNSUPPORTED")
        if reason is None:
            eligible[candidate.candidate_id] = candidate
        decisions.append(
            CandidateDecision(
                candidate_id=candidate.candidate_id,
                selected=False,
                mandatory=candidate.mandatory,
                rank=None,
                reason=reason or "MODEL_NOT_SELECTED",
            )
        )
    mandatory = [c.candidate_id for c in candidates if c.mandatory]
    capacity = min(config.budget.max_test_pairs, config.budget.max_c_runs // 2)
    if len(mandatory) > capacity:
        raise InputError("MANDATORY_BUDGET_EXCEEDED")
    optional = [c for c in eligible.values() if not c.mandatory]
    path_aliases = {f"path-{i + 1}": path.path_id for i, path in enumerate(analysis.impact_paths)}
    context: dict[str, JsonValue] = {
        "evidence": vector_query_context(handoff, analysis, catalog, "selection-v1"),
        "paths": [
            {
                "path_id": alias,
                "side": path.side,
                "support": path.support,
                "node_ids": [
                    change.node_id
                    for change in analysis.changes
                    if change.change_id in path.change_ids
                ],
                "limitations": list(path.limitations),
            }
            for alias, path in zip(path_aliases, analysis.impact_paths, strict=True)
        ],
        "cards": [
            {"card_key": key, "rank": hit.rank, "score": hit.score, "reason": hit.reason}
            for key, hit in hits.items()
        ],
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "description": c.description,
                "card_keys": list(c.card_keys),
            }
            for c in optional
        ],
        "optional_slots": capacity - len(mandatory),
    }
    context["card_evidence"] = [
        {
            "card_key": f"{c.card_id}@{c.version}",
            "title": c.title,
            "summary": c.summary,
            "claim_basis": c.claim_basis,
            "source_sha256": c.source_ref.sha256 if c.source_ref else None,
        }
        for c in cards
        if f"{c.card_id}@{c.version}" in hits
    ]
    if not optional or capacity == len(mandatory):
        generated = GenerationResult(
            Proposal(choices=[]),
            {
                "called": False,
                "reason": "NO_ELIGIBLE_OPTIONAL_TEST" if not optional else "NO_BUDGET",
            },
        )
    elif replay is not None:
        generated = replay
        if generated.metadata.get("context_sha256") != digest(json_bytes(context)):
            raise InputError("SELECTION_REPLAY_CONTEXT_MISMATCH")
    else:
        if provider is None or config.generation is None:
            raise InputError("GENERATION_PROVIDER_REQUIRED")
        generated = provider.propose(context)
    metadata = {**generated.metadata, "context_sha256": digest(json_bytes(context))}
    choices = generated.proposal.choices
    if len({c.candidate_id for c in choices}) != len(choices):
        raise InputError("GENERATION_CANDIDATE_DUPLICATE")
    selected = list(mandatory)
    by_decision = {d.candidate_id: d for d in decisions}
    for index, choice in enumerate(choices, 1):
        proposed_candidate = eligible.get(choice.candidate_id)
        if proposed_candidate is None or proposed_candidate.mandatory:
            raise InputError("GENERATION_CANDIDATE_NOT_ELIGIBLE")
        if (
            not choice.card_keys
            or not set(choice.card_keys).issubset(hits)
            or not set(choice.card_keys).issubset(proposed_candidate.card_keys)
        ):
            raise InputError("GENERATION_CARD_REFERENCE_INVALID")
        if not set(choice.path_ids).issubset(path_aliases):
            raise InputError("GENERATION_PATH_REFERENCE_INVALID")
        decision = by_decision[choice.candidate_id]
        decision.rank = index
        decision.card_keys = choice.card_keys
        decision.path_ids = [path_aliases[alias] for alias in choice.path_ids]
        if len(selected) < capacity:
            selected.append(choice.candidate_id)
            decision.selected = True
            decision.reason = choice.reason
        else:
            decision.reason = "BUDGET_EXCLUDED: " + choice.reason
    for candidate_id in mandatory:
        decision = by_decision[candidate_id]
        decision.selected = True
        decision.reason = "MANDATORY_POLICY_AND_NORMAL_COVERAGE"
    selection = PlanSelection(
        selection_id="pending",
        analysis_id=analysis.analysis_id,
        retrieval_id=retrieval.retrieval_id,
        config_sha256=digest(json_bytes(config.model_dump(mode="json"))),
        retrieval_sha256=digest(json_bytes(retrieval.model_dump(mode="json"))),
        kb_sha256=retrieval.kb_snapshot_ref.sha256,
        catalog_sha256=digest(json_bytes(catalog.model_dump(mode="json"))),
        candidate_catalog_sha256=digest(
            json_bytes([c.model_dump(mode="json") for c in candidates])
        ),
        search_status="HITS" if hits else "EMPTY_RETRIEVAL",
        generation=metadata,
        proposal=generated.proposal,
        decisions=decisions,
        selected_candidate_ids=selected,
        reserved_c_runs=2 * len(selected),
    )
    payload = selection.model_dump(mode="json")
    payload.pop("selection_id")
    selection.selection_id = "selection-" + digest(json_bytes(payload))[:24]
    return selection
