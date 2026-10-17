"""Offline keyword retrieval; no database or model dependencies."""

import re

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.cards import _ref, _unique, kb_bytes
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    Authorship,
    RetrievalHit,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
)
from flowdelta.pipeline_io import validate_input_fields


def author(analysis: AnalysisBundle, producer: str) -> Authorship:
    return Authorship(
        origin="MODULE_OUTPUT",
        producer=producer,
        note=f"Deterministic planning; analysis origin={analysis.authorship.origin}.",
    )


def context_fingerprint(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
) -> str:
    return digest(
        json_bytes(
            {
                "handoff": handoff.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json"),
                "catalog": catalog.model_dump(mode="json"),
            }
        )
    )


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    *,
    kb_path: str = "kb-snapshot.json",
    producer: str,
) -> RetrievalResult:
    cards = _unique(cards)
    profile = handoff.environment.extensions.get("flowdelta.semantic_profile")
    node_types = sorted(
        {node.node_type for wf in (handoff.before, handoff.after) for node in wf.nodes}
    )
    fields = sorted(
        {
            source.json_pointer.rsplit("/", 1)[-1]
            for change in analysis.changes
            for source in (change.before_source, change.after_source)
            if source is not None
        }
    )
    tokens = sorted(
        {token.casefold() for field in fields for token in re.findall(r"[\w-]+", field)}
    )
    templates = {(t.template_id, t.version): t for t in catalog.templates}
    excluded: dict[str, str] = {}
    candidates: list[tuple[ScenarioCard, float, str]] = []
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        template = templates.get((card.template_id, card.template_version))
        if card.runtime_profile != profile:
            excluded[key] = "RUNTIME_PROFILE_MISMATCH"
            continue
        if template is None:
            raise InputError("TEMPLATE_NOT_FOUND")
        if template.task_id != handoff.task.task_id or template.runtime_profile != profile:
            excluded[key] = "TEMPLATE_CONTEXT_MISMATCH"
            continue
        try:
            validate_input_fields(handoff.task.normal_input, template.input_fields)
        except InputError as exc:
            excluded[key] = exc.code
            continue
        overlap = sorted(set(card.node_types) & set(node_types))
        if card.node_types and not overlap:
            excluded[key] = "NODE_TYPE_MISMATCH"
            continue
        text = f"{card.title} {card.summary}".casefold()
        matched = [token for token in tokens if token in text]
        score = float(len(overlap) + len(matched))
        candidates.append(
            (card, score, f"task/profile matched; node_types={overlap}; field_keywords={matched}")
        )
    candidates.sort(key=lambda item: (-item[1], item[0].card_id, item[0].version))
    kb = kb_bytes(cards)
    fingerprint = digest(
        producer.encode("utf-8")
        + kb
        + json_bytes(analysis.model_dump(mode="json"))
        + json_bytes(catalog.model_dump(mode="json"))
        + json_bytes(handoff.model_dump(mode="json"))
    )[:24]
    return RetrievalResult(
        retrieval_id=f"retrieval-{fingerprint}",
        analysis_id=analysis.analysis_id,
        authorship=author(analysis, producer),
        kb_snapshot_ref=_ref(kb_path, kb, "knowledge-snapshot"),
        method="KEYWORD",
        model_version=None,
        query={
            "task_id": handoff.task.task_id,
            "runtime_profile": profile,
            "node_types": [str(value) for value in node_types],
            "changed_fields": [str(value) for value in fields],
            "rules_version": producer,
            "context_fingerprint": context_fingerprint(handoff, analysis, catalog),
        },
        hits=[
            RetrievalHit(
                card_id=card.card_id,
                card_version=card.version,
                rank=rank,
                score=score,
                applicability="MATCH" if set(card.node_types).issubset(node_types) else "PARTIAL",
                reason=reason,
            )
            for rank, (card, score, reason) in enumerate(candidates, 1)
        ],
        excluded_reasons=excluded,
    )
