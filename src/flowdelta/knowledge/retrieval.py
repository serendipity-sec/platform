"""Offline keyword retrieval plus a database-backed vector path loaded on demand."""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue

from flowdelta.contracts import Handoff, SourceLocation
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.cards import KB_SNAPSHOT_FILENAME, _KnowledgeSnapshot
from flowdelta.knowledge.embedding import (
    MAX_TEXT_BYTES,
    EmbeddingProfile,
    EmbeddingProvider,
    validate_embedding_batch,
)
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
from flowdelta.runtime.static_scan import StaticScanCatalog, StaticScanTemplate

Catalog = TemplateCatalog | StaticScanCatalog

if TYPE_CHECKING:
    from sqlalchemy import Engine

_SENSITIVE_FIELD = re.compile(
    r"credential|password|passwd|secret|token|api.?key|authorization|private.?key|access.?key",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImpactPathSignal:
    path_id: str
    node_types: frozenset[str]
    keywords: frozenset[str]
    support: str


def author(analysis: AnalysisBundle, producer: str) -> Authorship:
    return Authorship(
        origin="MODULE_OUTPUT",
        producer=producer,
        note=f"Deterministic planning; analysis origin={analysis.authorship.origin}.",
    )


def context_fingerprint(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: Catalog,
    producer: str,
) -> str:
    """Fingerprint planning inputs with either B's or C's registered catalog contract."""
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
    catalog: Catalog,
    *,
    selected_path_id: str | None = None,
) -> str:
    """Derive retrieval identity, binding static plans to their path-selection request."""
    selection_identity = (
        json_bytes({"selected_path_id": selected_path_id})
        if isinstance(catalog, StaticScanCatalog)
        else b""
    )
    fingerprint = digest(
        producer.encode("utf-8")
        + snapshot
        + json_bytes(analysis.model_dump(mode="json"))
        + json_bytes(catalog.model_dump(mode="json"))
        + json_bytes(handoff.model_dump(mode="json"))
        + selection_identity
    )[:24]
    return f"retrieval-{fingerprint}"


def vector_retrieval_identity(
    producer: str,
    snapshot: bytes,
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: Catalog,
    profile: dict[str, JsonValue],
    query_sha256: str,
    candidates: list[dict[str, JsonValue]],
    result_fingerprint: str,
    *,
    selected_path_id: str | None = None,
) -> str:
    fingerprint = digest(
        json_bytes(
            {
                "base_retrieval_id": retrieval_identity(
                    producer,
                    snapshot,
                    handoff,
                    analysis,
                    catalog,
                    selected_path_id=selected_path_id,
                ),
                "method": "VECTOR",
                "profile": profile,
                "query_sha256": query_sha256,
                "candidates": candidates,
                "result_fingerprint": result_fingerprint,
            }
        )
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
    templates: dict[tuple[str, str], Template | StaticScanTemplate],
    profile: object,
    node_types: Set[str],
    *,
    exclude_unregistered_template: bool = False,
    check_artifacts: bool = False,
) -> str | None:
    """Check card fit against normal templates or targets C actually scans."""
    if card.runtime_profile != profile:
        return "RUNTIME_PROFILE_MISMATCH"
    template = templates.get((card.template_id, card.template_version))
    if template is None:
        if exclude_unregistered_template:
            return "TEMPLATE_NOT_REGISTERED"
        raise InputError("TEMPLATE_NOT_FOUND")
    if isinstance(template, StaticScanTemplate):
        target_types = {target.node_type for target in template.targets}
        if card.node_types and not (set(card.node_types) & node_types & target_types):
            return "NODE_TYPE_MISMATCH"
        if not set(card.required_artifact_kinds).issubset(template.result_kinds):
            return "TEMPLATE_ARTIFACT_UNSUPPORTED"
        return None
    if template.task_id != handoff.task.task_id or template.runtime_profile != profile:
        return "TEMPLATE_CONTEXT_MISMATCH"
    try:
        validate_input_fields(handoff.task.normal_input, template.input_fields)
    except InputError as exc:
        return exc.code
    if check_artifacts and not set(card.required_artifact_kinds).issubset(
        template.required_artifact_kinds
    ):
        return "TEMPLATE_ARTIFACTS_INCOMPATIBLE"
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


def retrieval_query_context(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: Catalog,
    producer: str,
) -> dict[str, JsonValue]:
    profile, node_types, fields, _ = search_context(handoff, analysis)
    analysis_context, _, _, _ = analysis_retrieval_context(handoff, analysis)
    return {
        "task_id": handoff.task.task_id,
        "runtime_profile": profile,
        "node_types": [str(value) for value in node_types],
        "changed_fields": [str(value) for value in fields],
        "analysis_context": analysis_context,
        "rules_version": producer,
        "context_fingerprint": context_fingerprint(handoff, analysis, catalog, producer),
    }


def _redacted_marker(reason: str) -> dict[str, JsonValue]:
    return {"redacted": True, "reason": reason}


def _embedding_safe_value(value: JsonValue, source: SourceLocation | None, kind: str) -> JsonValue:
    if kind == "CREDENTIAL_CHANGED":
        return _redacted_marker("CREDENTIAL_CHANGE")
    if source is not None and _SENSITIVE_FIELD.search(str(source.json_pointer)):
        return _redacted_marker("SENSITIVE_SOURCE_FIELD")
    if isinstance(value, dict):
        safe: dict[str, JsonValue] = {}
        for key, item in value.items():
            if _SENSITIVE_FIELD.search(key):
                safe[key] = _redacted_marker("SENSITIVE_OBJECT_FIELD")
            else:
                safe[key] = _embedding_safe_value(item, None, kind)
        return safe
    if isinstance(value, list):
        return [_embedding_safe_value(item, None, kind) for item in value]
    return value


def vector_query_context(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: Catalog,
    producer: str,
) -> dict[str, JsonValue]:
    """Send bounded analysis values to a trusted endpoint; redaction is heuristic."""
    # Trace IDs and case names remain in RetrievalResult, never in model input.
    # Values are bounded/redacted using the same policy as the legacy query.
    return {
        "runtime_profile": handoff.environment.extensions.get("flowdelta.semantic_profile"),
        "node_types": cast(JsonValue, search_context(handoff, analysis)[1]),
        "task": {
            "purpose": handoff.task.purpose,
            "success_conditions": list(handoff.task.success_conditions),
            "protected_resources": list(handoff.task.protected_resources),
        },
        "policies": [
            {
                "condition": rule.condition,
                "resource": rule.resource,
                "subject": rule.subject,
                "exceptions": list(rule.exceptions),
            }
            for rule in handoff.policy.rules
        ],
        "impact_paths": [
            {
                "side": path.side,
                "support": path.support,
                "policy_rule_ids": list(path.policy_rule_ids),
                "limitations": list(path.limitations),
            }
            for path in analysis.impact_paths
        ],
        "limitations": list(analysis.limitations),
        "change_values": [
            {
                "kind": change.kind,
                "node_id": change.node_id,
                "before_value": _embedding_safe_value(
                    change.before_value, change.before_source, change.kind
                ),
                "after_value": _embedding_safe_value(
                    change.after_value, change.after_source, change.kind
                ),
            }
            for change in sorted(analysis.changes, key=lambda item: item.change_id)
        ],
    }


def vector_query_text(query_context: dict[str, JsonValue]) -> str:
    try:
        data = json_bytes(query_context)
        text = data.decode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise InputError("VECTOR_QUERY_INVALID") from None
    if not text or len(data) > MAX_TEXT_BYTES:
        raise InputError("VECTOR_QUERY_TOO_LARGE")
    return text


def embedding_profile_metadata(profile: EmbeddingProfile) -> dict[str, JsonValue]:
    return {
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "model": profile.model,
        "model_digest": profile.model_digest,
        "dimensions": profile.dimensions,
        "query_instruction_version": profile.query_instruction_version,
        "normalized": profile.normalized,
    }


def analysis_retrieval_context(
    handoff: Handoff,
    analysis: AnalysisBundle,
) -> tuple[dict[str, JsonValue], set[str], list[str], list[ImpactPathSignal]]:
    """Keep A's path evidence and limits visible to retrieval and the resulting plan."""
    nodes_by_side = {
        "before": {node.node_id: node.node_type for node in handoff.before.nodes},
        "after": {node.node_id: node.node_type for node in handoff.after.nodes},
    }
    graphs_by_side = {
        "before": analysis.before_graph,
        "after": analysis.after_graph,
    }
    edges_by_side = {
        side: {edge.edge_id: edge for edge in graph.edges} for side, graph in graphs_by_side.items()
    }
    graph_to_workflow_nodes_by_side = {
        side: (
            {
                node.node_id: node.workflow_node_id
                for node in graph.nodes
                if node.kind == "WORKFLOW_NODE" and node.workflow_node_id is not None
            }
            if graph.nodes
            else {node_id: node_id for node_id in graph.node_ids}
        )
        for side, graph in graphs_by_side.items()
    }
    changes_by_id = {change.change_id: change for change in analysis.changes}
    impacted_node_types: set[str] = set()
    impact_tokens: set[str] = set()
    referenced_change_ids: set[str] = set()
    unresolved_path_change_ids: set[str] = set()
    path_refs: list[dict[str, JsonValue]] = []
    path_signals: list[ImpactPathSignal] = []

    for path in analysis.impact_paths:
        path_node_types: set[str] = set()
        path_tokens: set[str] = set()
        path_refs.append(
            {
                "path_id": path.path_id,
                "side": path.side,
                "change_ids": list(path.change_ids),
                "edge_ids": list(path.edge_ids),
                "task_ids": list(path.task_ids),
                "policy_rule_ids": list(path.policy_rule_ids),
                "support": path.support,
                "basis": path.basis,
                "limitations": list(path.limitations),
            }
        )
        for change_id in path.change_ids:
            change = changes_by_id.get(change_id)
            if change is None:
                unresolved_path_change_ids.add(change_id)
                continue
            referenced_change_ids.add(change_id)
            if change.node_id is not None:
                node_type = nodes_by_side[path.side].get(change.node_id)
                if node_type is not None:
                    impacted_node_types.add(node_type)
                    path_node_types.add(node_type)
            for source in (change.before_source, change.after_source):
                if source is None:
                    continue
                field = source.json_pointer.rsplit("/", 1)[-1]
                tokens = {token.casefold() for token in re.findall(r"[\w-]+", field)}
                impact_tokens.update(tokens)
                path_tokens.update(tokens)
        for edge_id in path.edge_ids:
            edge = edges_by_side[path.side].get(edge_id)
            if edge is None:
                continue
            for endpoint_id in (edge.source_node_id, edge.target_node_id):
                workflow_node_id = graph_to_workflow_nodes_by_side[path.side].get(endpoint_id)
                if workflow_node_id is None:
                    continue
                node_type = nodes_by_side[path.side].get(workflow_node_id)
                if node_type is not None:
                    impacted_node_types.add(node_type)
                    path_node_types.add(node_type)
        path_signals.append(
            ImpactPathSignal(
                path_id=path.path_id,
                node_types=frozenset(path_node_types),
                keywords=frozenset(path_tokens),
                support=path.support,
            )
        )

    path_refs.sort(key=lambda item: str(item["path_id"]))
    path_signals.sort(key=lambda item: item.path_id)
    change_sources: list[dict[str, JsonValue]] = []
    for change in sorted(analysis.changes, key=lambda item: item.change_id):
        change_sources.append(
            {
                "change_id": change.change_id,
                "kind": change.kind,
                "node_id": change.node_id,
                "before_source": (
                    change.before_source.model_dump(mode="json")
                    if change.before_source is not None
                    else None
                ),
                "after_source": (
                    change.after_source.model_dump(mode="json")
                    if change.after_source is not None
                    else None
                ),
            }
        )

    snapshots_by_id = {snapshot.snapshot_id: snapshot for snapshot in handoff.snapshots.snapshots}
    snapshot_hashes: list[dict[str, JsonValue]] = []
    for snapshot_id in (analysis.before_snapshot_id, analysis.after_snapshot_id):
        snapshot = snapshots_by_id.get(snapshot_id)
        snapshot_hashes.append(
            {
                "snapshot_id": snapshot_id,
                "side": snapshot.side if snapshot is not None else None,
                "raw_sha256": snapshot.raw_sha256 if snapshot is not None else None,
                "structural_sha256": (snapshot.structural_sha256 if snapshot is not None else None),
                "semantic_sha256": snapshot.semantic_sha256 if snapshot is not None else None,
                "semantic_hash_version": (
                    snapshot.semantic_hash_version if snapshot is not None else None
                ),
                "semantic_hash_scope": (
                    snapshot.semantic_hash_scope if snapshot is not None else None
                ),
            }
        )

    context: dict[str, JsonValue] = {
        "case_id": analysis.case_id,
        "before_snapshot_id": analysis.before_snapshot_id,
        "after_snapshot_id": analysis.after_snapshot_id,
        "analysis_id": analysis.analysis_id,
        "analysis_schema_version": analysis.schema_version,
        "analysis_producer": analysis.authorship.producer,
        "analysis_content_sha256": digest(json_bytes(analysis.model_dump(mode="json"))),
        "analysis_hash_basis": "canonical-json-v1",
        "snapshot_hashes": cast(JsonValue, snapshot_hashes),
        "impact_paths": cast(JsonValue, path_refs),
        "changes": cast(JsonValue, change_sources),
        "unlinked_change_ids": cast(JsonValue, sorted(set(changes_by_id) - referenced_change_ids)),
        "unresolved_path_change_ids": cast(JsonValue, sorted(unresolved_path_change_ids)),
        "limitations": list(analysis.limitations),
        "graph_limits": analysis.extensions.get("flowdelta.graph.limits"),
        "uncertain_impact_path_ids": [
            str(path["path_id"])
            for path in path_refs
            if path["support"] != "SUPPORTED" or path["limitations"]
        ],
    }
    return context, impacted_node_types, sorted(impact_tokens), path_signals


def hit_details(
    card: ScenarioCard,
    node_types: Set[str],
    tokens: list[str],
    *,
    impacted_node_types: Set[str] | None = None,
    impact_tokens: list[str] | None = None,
    impact_path_signals: list[ImpactPathSignal] | None = None,
) -> tuple[float, Literal["MATCH", "PARTIAL"], str]:
    overlap = sorted(set(card.node_types) & node_types)
    text = f"{card.title} {card.summary}".casefold()
    matched = [token for token in tokens if token in text]
    impact_overlap = sorted(set(card.node_types) & (impacted_node_types or set()))
    impact_matched = [token for token in impact_tokens or [] if token in text]
    score = float(len(overlap) + len(matched) + len(impact_overlap) + len(impact_matched))
    matched_paths = []
    for path in impact_path_signals or []:
        path_node_overlap = sorted(set(card.node_types) & path.node_types)
        path_keywords = sorted(token for token in path.keywords if token in text)
        if path_node_overlap or path_keywords:
            matched_paths.append(f"{path.path_id}(support={path.support})")
    applicability: Literal["MATCH", "PARTIAL"] = (
        "MATCH" if set(card.node_types).issubset(node_types) else "PARTIAL"
    )
    reason = f"task/profile matched; node_types={overlap}; field_keywords={matched}"
    if impact_overlap or impact_matched:
        reason += f"; impact_node_types={impact_overlap}; impact_keywords={impact_matched}"
    if matched_paths:
        shown_paths = matched_paths[:8]
        reason += f"; impact_paths={shown_paths}"
        if len(matched_paths) > len(shown_paths):
            reason += f"; additional_impact_paths={len(matched_paths) - len(shown_paths)}"
    return score, applicability, reason


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: Catalog,
    *,
    kb_path: str = KB_SNAPSHOT_FILENAME,
    producer: str,
    selected_path_id: str | None = None,
) -> RetrievalResult:
    """Create a retrieval result whose static identity includes the requested path ID."""
    return _retrieve_snapshot(
        handoff,
        analysis,
        _KnowledgeSnapshot.prepare(cards, kb_path),
        catalog,
        producer=producer,
        selected_path_id=selected_path_id,
    )


def _retrieve_snapshot(
    handoff: Handoff,
    analysis: AnalysisBundle,
    snapshot: _KnowledgeSnapshot,
    catalog: Catalog,
    *,
    producer: str,
    selected_path_id: str | None = None,
) -> RetrievalResult:
    """Rank cards and include static path selection in the retrieval artifact identity."""
    cards = snapshot.cards
    profile, node_types, fields, tokens = search_context(handoff, analysis)
    _, impacted_node_types, impact_tokens, impact_path_signals = analysis_retrieval_context(
        handoff, analysis
    )
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
        score, applicability, reason = hit_details(
            card,
            node_type_set,
            tokens,
            impacted_node_types=impacted_node_types,
            impact_tokens=impact_tokens,
            impact_path_signals=impact_path_signals,
        )
        candidates.append((card, score, applicability, reason))
    candidates.sort(key=lambda item: (-item[1], item[0].card_id, item[0].version))
    result = RetrievalResult(
        retrieval_id=retrieval_identity(
            producer,
            snapshot.data,
            handoff,
            analysis,
            catalog,
            selected_path_id=selected_path_id,
        ),
        analysis_id=analysis.analysis_id,
        authorship=author(analysis, producer),
        kb_snapshot_ref=snapshot.reference,
        method="KEYWORD",
        model_version=None,
        query=retrieval_query_context(handoff, analysis, catalog, producer),
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


def _vector_card_hash(card: ScenarioCard) -> str:
    return digest(json_bytes(card.model_dump(mode="json")))


def _vector_hit_reason(score: float) -> str:
    return f"Profile-scoped cosine similarity={score:.17g}."


def retrieve_vector_snapshot(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: Catalog,
    *,
    engine: Engine,
    provider: EmbeddingProvider,
    limit: int,
    kb_path: str,
    producer: str,
    selected_path_id: str | None = None,
    pinned_cards: Mapping[tuple[str, str], str] | None = None,
    expected_profile_id: str | None = None,
) -> tuple[_KnowledgeSnapshot, RetrievalResult]:
    from flowdelta.knowledge.card_catalog import (
        MAX_SEARCH_LIMIT,
        CardCandidate,
        search_cards,
    )

    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise InputError("CARD_CATALOG_SEARCH_LIMIT_INVALID")
    if not callable(getattr(provider, "embed", None)):
        raise InputError("EMBEDDING_PROVIDER_INVALID")

    context_query = retrieval_query_context(handoff, analysis, catalog, producer)
    query_text = vector_query_text(vector_query_context(handoff, analysis, catalog, producer))
    query_batch = validate_embedding_batch(
        provider.embed([query_text], "QUERY"), expected_count=1, dimensions=1024
    )
    profile, query_vector = query_batch.profile, query_batch.vectors[0]
    if expected_profile_id is not None and profile.profile_id != expected_profile_id:
        raise InputError("RETRIEVAL_PROFILE_MISMATCH")
    if pinned_cards is None:
        search_candidates = search_cards(engine, query_vector, profile, limit=limit)
    else:
        search_candidates = search_cards(
            engine, query_vector, profile, limit=limit, pinned_cards=pinned_cards
        )
    if not isinstance(search_candidates, list):
        raise InputError("CARD_CATALOG_SEARCH_RESULT_INVALID")

    checked: list[tuple[ScenarioCard, str, float]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in search_candidates:
        if not isinstance(candidate, CardCandidate) or not isinstance(candidate.card, ScenarioCard):
            raise InputError("CARD_CATALOG_SEARCH_RESULT_INVALID")
        card = candidate.card.model_copy(deep=True)
        identity = (card.card_id, card.version)
        if identity in seen:
            raise InputError("CARD_CATALOG_CANDIDATE_DUPLICATE")
        seen.add(identity)
        card_hash = _vector_card_hash(card)
        if pinned_cards is not None and pinned_cards.get(identity) != card_hash:
            raise InputError("CARD_CATALOG_PIN_MISMATCH")
        if candidate.content_sha256 != card_hash:
            raise InputError("CARD_CATALOG_CARD_INTEGRITY_FAILED")
        if type(candidate.score) not in {int, float}:
            raise InputError("RETRIEVAL_SCORE_INVALID")
        try:
            score = float(candidate.score)
        except (OverflowError, ValueError):
            raise InputError("RETRIEVAL_SCORE_INVALID") from None
        if not isfinite(score) or not -1.0 <= score <= 1.0:
            raise InputError("RETRIEVAL_SCORE_INVALID")
        checked.append((card, card_hash, score))

    checked.sort(key=lambda item: (-item[2], item[0].card_id, item[0].version))
    snapshot = _KnowledgeSnapshot.prepare([item[0] for item in checked], kb_path)
    runtime_profile, node_types, _, _ = search_context(handoff, analysis)
    node_type_set = set(node_types)
    templates = {(item.template_id, item.version): item for item in catalog.templates}
    vector_candidates: list[dict[str, JsonValue]] = []
    excluded: dict[str, str] = {}
    hits: list[RetrievalHit] = []
    for rank, (card, card_hash, score) in enumerate(checked, 1):
        reason = exclusion_reason(
            card,
            handoff,
            templates,
            runtime_profile,
            node_type_set,
            exclude_unregistered_template=True,
            check_artifacts=True,
        )
        card_key = f"{card.card_id}@{card.version}"
        vector_candidates.append(
            {
                "rank": rank,
                "card_id": card.card_id,
                "card_version": card.version,
                "card_sha256": card_hash,
                "similarity": score,
                "excluded_reason": reason,
            }
        )
        if reason is not None:
            excluded[card_key] = reason
        else:
            applicability: Literal["MATCH", "PARTIAL"] = (
                "MATCH" if set(card.node_types).issubset(node_type_set) else "PARTIAL"
            )
            hits.append(
                RetrievalHit(
                    card_id=card.card_id,
                    card_version=card.version,
                    rank=len(hits) + 1,
                    score=score,
                    applicability=applicability,
                    reason=_vector_hit_reason(score),
                )
            )

    profile_metadata = embedding_profile_metadata(profile)
    query_sha256 = digest(query_text.encode("utf-8"))
    result = RetrievalResult(
        retrieval_id="retrieval-pending",
        analysis_id=analysis.analysis_id,
        authorship=author(analysis, producer),
        kb_snapshot_ref=snapshot.reference,
        method="VECTOR",
        model_version=profile.model_digest,
        query={
            **context_query,
            "vector": {
                "profile": profile_metadata,
                "query_sha256": query_sha256,
                "candidates": cast(JsonValue, vector_candidates),
                "search_scope": (
                    [
                        {"card_id": key[0], "version": key[1], "sha256": value}
                        for key, value in sorted(pinned_cards.items())
                    ]
                    if pinned_cards is not None
                    else None
                ),
            },
        },
        hits=hits,
        excluded_reasons=excluded,
    )
    result_fingerprint = retrieval_content_fingerprint(result)
    result.query["result_fingerprint"] = result_fingerprint
    result.retrieval_id = vector_retrieval_identity(
        producer,
        snapshot.data,
        handoff,
        analysis,
        catalog,
        profile_metadata,
        query_sha256,
        vector_candidates,
        result_fingerprint,
        selected_path_id=selected_path_id,
    )
    return snapshot, result
