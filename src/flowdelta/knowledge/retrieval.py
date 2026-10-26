"""Offline keyword retrieval; no database or model dependencies."""

import re
from collections.abc import Set
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.cards import KB_SNAPSHOT_FILENAME, _KnowledgeSnapshot
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    Authorship,
    RetrievalHit,
    RetrievalResult,
    ScenarioCard,
    Template,
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
    producer: str,
) -> str:
    return digest(
        json_bytes(
            {
                "producer": producer,
                "handoff": handoff.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json"),
                "catalog": catalog.model_dump(mode="json"),
            }
        )
    )


def retrieval_identity(
    producer: str,
    snapshot: bytes,
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
) -> str:
    fingerprint = digest(
        producer.encode("utf-8")
        + snapshot
        + json_bytes(analysis.model_dump(mode="json"))
        + json_bytes(catalog.model_dump(mode="json"))
        + json_bytes(handoff.model_dump(mode="json"))
    )[:24]
    return f"retrieval-{fingerprint}"


def retrieval_content_fingerprint(result: RetrievalResult) -> str:
    """Hash the serialized search result so composition can detect later edits."""
    payload = result.model_dump(mode="json")
    payload.pop("retrieval_id", None)
    payload["query"].pop("result_fingerprint", None)
    return digest(json_bytes(payload))


def exclusion_reason(
    card: ScenarioCard,
    handoff: Handoff,
    templates: dict[tuple[str, str], Template],
    profile: object,
    node_types: Set[str],
) -> str | None:
    if card.runtime_profile != profile:
        return "RUNTIME_PROFILE_MISMATCH"
    template = templates.get((card.template_id, card.template_version))
    if template is None:
        raise InputError("TEMPLATE_NOT_FOUND")
    if template.task_id != handoff.task.task_id or template.runtime_profile != profile:
        return "TEMPLATE_CONTEXT_MISMATCH"
    try:
        validate_input_fields(handoff.task.normal_input, template.input_fields)
    except InputError as exc:
        return exc.code
    overlap = set(card.node_types) & node_types
    if card.node_types and not overlap:
        return "NODE_TYPE_MISMATCH"
    return None


def search_context(
    handoff: Handoff,
    analysis: AnalysisBundle,
) -> tuple[JsonValue, list[str], list[str], list[str]]:
    profile = handoff.environment.extensions.get("flowdelta.semantic_profile")
    node_types = sorted(
        {node.node_type for workflow in (handoff.before, handoff.after) for node in workflow.nodes}
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
    return profile, node_types, fields, tokens


def hit_details(
    card: ScenarioCard,
    node_types: Set[str],
    tokens: list[str],
) -> tuple[float, Literal["MATCH", "PARTIAL"], str]:
    overlap = sorted(set(card.node_types) & node_types)
    text = f"{card.title} {card.summary}".casefold()
    matched = [token for token in tokens if token in text]
    score = float(len(overlap) + len(matched))
    applicability: Literal["MATCH", "PARTIAL"] = (
        "MATCH" if set(card.node_types).issubset(node_types) else "PARTIAL"
    )
    reason = f"task/profile matched; node_types={overlap}; field_keywords={matched}"
    return score, applicability, reason


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    *,
    kb_path: str = KB_SNAPSHOT_FILENAME,
    producer: str,
) -> RetrievalResult:
    return _retrieve_snapshot(
        handoff, analysis, _KnowledgeSnapshot.prepare(cards, kb_path), catalog, producer=producer
    )


def _retrieve_snapshot(
    handoff: Handoff,
    analysis: AnalysisBundle,
    snapshot: _KnowledgeSnapshot,
    catalog: TemplateCatalog,
    *,
    producer: str,
) -> RetrievalResult:
    cards = snapshot.cards
    profile, node_types, fields, tokens = search_context(handoff, analysis)
    node_type_set = set(node_types)
    templates = {(t.template_id, t.version): t for t in catalog.templates}
    excluded: dict[str, str] = {}
    candidates: list[tuple[ScenarioCard, float, Literal["MATCH", "PARTIAL"], str]] = []
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        reason = exclusion_reason(card, handoff, templates, profile, node_type_set)
        if reason is not None:
            excluded[key] = reason
            continue
        score, applicability, reason = hit_details(card, node_type_set, tokens)
        candidates.append((card, score, applicability, reason))
    candidates.sort(key=lambda item: (-item[1], item[0].card_id, item[0].version))
    result = RetrievalResult(
        retrieval_id=retrieval_identity(producer, snapshot.data, handoff, analysis, catalog),
        analysis_id=analysis.analysis_id,
        authorship=author(analysis, producer),
        kb_snapshot_ref=snapshot.reference,
        method="KEYWORD",
        model_version=None,
        query={
            "task_id": handoff.task.task_id,
            "runtime_profile": profile,
            "node_types": [str(value) for value in node_types],
            "changed_fields": [str(value) for value in fields],
            "rules_version": producer,
            "context_fingerprint": context_fingerprint(handoff, analysis, catalog, producer),
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
            for rank, (card, score, applicability, reason) in enumerate(candidates, 1)
        ],
        excluded_reasons=excluded,
    )
    result.query["result_fingerprint"] = retrieval_content_fingerprint(result)
    return result
