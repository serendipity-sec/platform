"""Pure composition from a fixed retrieval result and canonical card snapshot."""

from math import isfinite
from typing import cast

from pydantic import JsonValue

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, digest
from flowdelta.knowledge.cards import _KnowledgeSnapshot
from flowdelta.knowledge.embedding import EmbeddingProfile
from flowdelta.knowledge.retrieval import (
    _vector_card_hash,
    _vector_hit_reason,
    analysis_retrieval_context,
    author,
    context_fingerprint,
    embedding_profile_metadata,
    exclusion_reason,
    hit_details,
    retrieval_content_fingerprint,
    retrieval_identity,
    retrieval_query_context,
    search_context,
    vector_query_context,
    vector_query_text,
    vector_retrieval_identity,
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


def _validate_vector_retrieval(
    handoff: Handoff,
    analysis: AnalysisBundle,
    snapshot: _KnowledgeSnapshot,
    catalog: TemplateCatalog,
    retrieval: RetrievalResult,
    producer: str,
) -> None:
    vector = retrieval.query.get("vector")
    if not isinstance(vector, dict) or set(vector) != {"profile", "query_sha256", "candidates"}:
        raise InputError("RETRIEVAL_VECTOR_METADATA_INVALID")
    metadata_value = vector.get("profile")
    profile_fields = {
        "profile_id",
        "provider",
        "model",
        "model_digest",
        "dimensions",
        "query_instruction_version",
        "normalized",
    }
    if not isinstance(metadata_value, dict) or set(metadata_value) != profile_fields:
        raise InputError("RETRIEVAL_PROFILE_MISMATCH")
    metadata = metadata_value
    if (
        any(
            type(metadata.get(name)) is not str or not metadata.get(name)
            for name in (
                "profile_id",
                "provider",
                "model",
                "model_digest",
                "query_instruction_version",
            )
        )
        or type(metadata.get("dimensions")) is not int
        or metadata.get("dimensions") != 1024
        or type(metadata.get("normalized")) is not bool
    ):
        raise InputError("RETRIEVAL_PROFILE_MISMATCH")
    profile = EmbeddingProfile(
        provider=cast(str, metadata["provider"]),
        model=cast(str, metadata["model"]),
        model_digest=cast(str, metadata["model_digest"]),
        dimensions=cast(int, metadata["dimensions"]),
        query_instruction_version=cast(str, metadata["query_instruction_version"]),
        normalized=cast(bool, metadata["normalized"]),
    )
    if (
        metadata != embedding_profile_metadata(profile)
        or retrieval.model_version != profile.model_digest
    ):
        raise InputError("RETRIEVAL_PROFILE_MISMATCH")

    query_hash = vector.get("query_sha256")
    if type(query_hash) is not str or query_hash != digest(
        vector_query_text(vector_query_context(handoff, analysis, catalog, producer)).encode(
            "utf-8"
        )
    ):
        raise InputError("RETRIEVAL_QUERY_MISMATCH")
    records_value = vector.get("candidates")
    if not isinstance(records_value, list):
        raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
    records: list[dict[str, JsonValue]] = []

    record_keys = {
        "rank",
        "card_id",
        "card_version",
        "card_sha256",
        "similarity",
        "excluded_reason",
    }
    by_key = {(card.card_id, card.version): card for card in snapshot.cards}
    seen: set[tuple[str, str]] = set()
    checked: list[dict[str, JsonValue]] = []
    for record_value in records_value:
        if not isinstance(record_value, dict) or set(record_value) != record_keys:
            raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
        record = record_value
        records.append(record)
        card_id = record.get("card_id")
        card_version = record.get("card_version")
        rank = record.get("rank")
        card_hash = record.get("card_sha256")
        similarity = record.get("similarity")
        excluded_reason = record.get("excluded_reason")
        if (
            type(card_id) is not str
            or type(card_version) is not str
            or type(rank) is not int
            or type(card_hash) is not str
            or type(similarity) not in {int, float}
            or (excluded_reason is not None and type(excluded_reason) is not str)
        ):
            raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
        try:
            score = float(cast(int | float, similarity))
        except (OverflowError, ValueError):
            raise InputError("RETRIEVAL_SCORE_INVALID") from None
        if not isfinite(score) or not -1.0 <= score <= 1.0:
            raise InputError("RETRIEVAL_SCORE_INVALID")
        key = (card_id, card_version)
        card = by_key.get(key)
        if key in seen or card is None or card_hash != _vector_card_hash(card):
            raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
        seen.add(key)
        checked.append(
            {
                "rank": rank,
                "card_id": card_id,
                "card_version": card_version,
                "card_sha256": card_hash,
                "similarity": score,
                "excluded_reason": excluded_reason,
            }
        )

    if seen != set(by_key):
        raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
    ordered = sorted(
        checked,
        key=lambda item: (
            -cast(float, item["similarity"]),
            cast(str, item["card_id"]),
            cast(str, item["card_version"]),
        ),
    )
    if checked != ordered or [cast(int, item["rank"]) for item in checked] != list(
        range(1, len(checked) + 1)
    ):
        raise InputError("RETRIEVAL_RANK_INVALID")

    semantic_profile, node_types, _, _ = search_context(handoff, analysis)
    templates = {(item.template_id, item.version): item for item in catalog.templates}
    expected_excluded: dict[str, str] = {}
    eligible: list[dict[str, JsonValue]] = []
    for record in checked:
        key = (cast(str, record["card_id"]), cast(str, record["card_version"]))
        card = by_key[key]
        reason = exclusion_reason(
            card,
            handoff,
            templates,
            semantic_profile,
            set(node_types),
            exclude_unregistered_template=True,
            check_artifacts=True,
        )
        if reason != cast(str | None, record["excluded_reason"]):
            raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")
        if reason is None:
            eligible.append(record)
        else:
            expected_excluded[f"{card.card_id}@{card.version}"] = reason
    if retrieval.excluded_reasons != expected_excluded:
        raise InputError("RETRIEVAL_CANDIDATE_MISMATCH")

    if len(eligible) != len(retrieval.hits):
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    for rank, (record, hit) in enumerate(zip(eligible, retrieval.hits, strict=True), 1):
        card = by_key[(cast(str, record["card_id"]), cast(str, record["card_version"]))]
        applicability = "MATCH" if set(card.node_types).issubset(node_types) else "PARTIAL"
        score = cast(float, record["similarity"])
        if (
            (hit.card_id, hit.card_version) != (card.card_id, card.version)
            or hit.rank != rank
            or hit.score != score
            or hit.applicability != applicability
            or hit.reason != _vector_hit_reason(score)
        ):
            raise InputError("RETRIEVAL_CONTENT_MISMATCH")

    result_fingerprint_value = retrieval.query.get("result_fingerprint")
    if type(
        result_fingerprint_value
    ) is not str or result_fingerprint_value != retrieval_content_fingerprint(retrieval):
        raise InputError("RETRIEVAL_CONTENT_MISMATCH")
    result_fingerprint = result_fingerprint_value
    if retrieval.retrieval_id != vector_retrieval_identity(
        producer,
        snapshot.data,
        handoff,
        analysis,
        catalog,
        metadata,
        query_hash,
        records,
        result_fingerprint,
    ):
        raise InputError("RETRIEVAL_ID_MISMATCH")


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
    analysis_context, impacted_node_types, impact_tokens, impact_path_signals = (
        analysis_retrieval_context(handoff, analysis)
    )
    expected_query = retrieval_query_context(handoff, analysis, catalog, producer)
    if retrieval.method == "KEYWORD":
        recorded_query = {
            key: value for key, value in retrieval.query.items() if key != "result_fingerprint"
        }
        if recorded_query != expected_query:
            raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
        if retrieval.model_version is not None:
            raise InputError("RETRIEVAL_METHOD_MISMATCH")
    elif retrieval.method == "VECTOR":
        recorded_query = {
            key: value
            for key, value in retrieval.query.items()
            if key not in {"result_fingerprint", "vector"}
        }
        if recorded_query != expected_query:
            raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    else:
        raise InputError("RETRIEVAL_METHOD_MISMATCH")
    expected = snapshot.reference
    if (retrieval.kb_snapshot_ref.sha256, retrieval.kb_snapshot_ref.size_bytes) != (
        expected.sha256,
        expected.size_bytes,
    ):
        raise InputError("RETRIEVAL_SNAPSHOT_MISMATCH")
    if retrieval.method == "KEYWORD" and retrieval.retrieval_id != retrieval_identity(
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
    if retrieval.method == "VECTOR":
        _validate_vector_retrieval(
            handoff,
            analysis,
            snapshot,
            catalog,
            retrieval,
            producer,
        )
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        reason = exclusion_reason(
            card,
            handoff,
            templates,
            profile,
            node_type_set,
            exclude_unregistered_template=retrieval.method == "VECTOR",
            check_artifacts=retrieval.method == "VECTOR",
        )
        if key in excluded_keys:
            if reason is None or reason != retrieval.excluded_reasons[key]:
                raise InputError("RETRIEVAL_CARD_MISMATCH")
        elif reason is not None:
            raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    for hit in retrieval.hits:
        card = by_card[(hit.card_id, hit.card_version)]
        if retrieval.method == "KEYWORD":
            score, applicability, reason = hit_details(
                card,
                node_type_set,
                tokens,
                impacted_node_types=impacted_node_types,
                impact_tokens=impact_tokens,
                impact_path_signals=impact_path_signals,
            )
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
            "analysis_context": analysis_context,
            "security_outcome": None,
        },
    )
    validate_pipeline(handoff, analysis, catalog, plan)
    return plan
