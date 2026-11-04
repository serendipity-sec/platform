"""Immutable shared ScenarioCard storage and profile-scoped pgvector search."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import (
    MAX_FILE_BYTES,
    InputError,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.knowledge.embedding import (
    MAX_TEXT_BYTES,
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingProvider,
    validate_embedding_batch,
    validate_embedding_profile,
    validate_embedding_vector,
)
from flowdelta.pipeline_contracts import ScenarioCard

SUPPORTED_DIMENSIONS = 1024
MAX_SEARCH_LIMIT = 100
MAX_INDEX_BATCH_SIZE = 16
_PROFILE_LOCK_SQL = """
    SELECT pg_try_advisory_lock(
        hashtextextended('flowdelta.card-catalog:' || :profile_id, 0)
    )
"""
_PROFILE_UNLOCK_SQL = """
    SELECT pg_advisory_unlock(
        hashtextextended('flowdelta.card-catalog:' || :profile_id, 0)
    )
"""


@dataclass(frozen=True)
class CardIndexResult:
    indexed: int
    reused: int


@dataclass(frozen=True)
class CardCandidate:
    card: ScenarioCard
    content_sha256: str
    score: float


@dataclass(frozen=True)
class _PreparedCard:
    card: ScenarioCard
    payload: bytes
    content_sha256: str
    embedding_text: str


def index_cards(
    engine: Engine,
    cards: Sequence[ScenarioCard],
    provider: EmbeddingProvider,
    profile: EmbeddingProfile,
    *,
    batch_size: int = MAX_INDEX_BATCH_SIZE,
) -> CardIndexResult:
    """Store immutable cards and embed only identities missing this exact profile."""
    _validate_profile(profile)
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_INDEX_BATCH_SIZE:
        raise InputError("CARD_CATALOG_BATCH_SIZE_INVALID")
    if not callable(getattr(provider, "embed", None)):
        raise InputError("EMBEDDING_PROVIDER_INVALID")
    prepared = _prepare_cards(cards)
    if not prepared:
        return CardIndexResult(indexed=0, reused=0)

    try:
        with _profile_lock(engine, profile) as connection:
            return _index_cards_locked(connection, prepared, provider, profile, batch_size)
    except SQLAlchemyError:
        raise InputError("CARD_CATALOG_STORE_FAILED") from None


def _index_cards_locked(
    connection: Connection,
    prepared: list[_PreparedCard],
    provider: EmbeddingProvider,
    profile: EmbeddingProfile,
    batch_size: int,
) -> CardIndexResult:
    missing: list[_PreparedCard] = []
    indexed = 0
    with connection.begin():
        profile_row = (
            connection.execute(
                text("""
                    SELECT profile_id, provider, model, model_digest, dimensions,
                           query_instruction_version, normalized
                    FROM knowledge.b_embedding_profile
                    WHERE profile_id=:profile_id
                """),
                {"profile_id": profile.profile_id},
            )
            .mappings()
            .one_or_none()
        )
        if profile_row is not None:
            _check_profile_row(profile_row, profile)

        for item in prepared:
            connection.execute(
                text("""
                    INSERT INTO knowledge.b_scenario_card (
                        card_id, card_version, content_sha256,
                        payload_size_bytes, payload_bytes
                    ) VALUES (
                        :card_id, :card_version, :content_sha256,
                        :payload_size_bytes, :payload_bytes
                    )
                    ON CONFLICT (card_id, card_version) DO NOTHING
                """),
                {
                    "card_id": item.card.card_id,
                    "card_version": item.card.version,
                    "content_sha256": item.content_sha256,
                    "payload_size_bytes": len(item.payload),
                    "payload_bytes": item.payload,
                },
            )
            stored = (
                connection.execute(
                    text("""
                        SELECT content_sha256, payload_size_bytes, payload_bytes
                        FROM knowledge.b_scenario_card
                        WHERE card_id=:card_id AND card_version=:card_version
                    """),
                    {"card_id": item.card.card_id, "card_version": item.card.version},
                )
                .mappings()
                .one_or_none()
            )
            if stored is None:
                raise InputError("KNOWLEDGE_CARD_STORE_FAILED")
            _check_stored_card(stored, item)

            embedding_hash = connection.execute(
                text("""
                    SELECT content_sha256
                    FROM knowledge.b_scenario_card_embedding
                    WHERE card_id=:card_id AND card_version=:card_version
                      AND profile_id=:profile_id
                """),
                {
                    "card_id": item.card.card_id,
                    "card_version": item.card.version,
                    "profile_id": profile.profile_id,
                },
            ).scalar_one_or_none()
            if embedding_hash is None:
                missing.append(item)
            elif embedding_hash != item.content_sha256:
                raise InputError("CARD_CATALOG_EMBEDDING_INTEGRITY_FAILED")

    if not missing:
        return CardIndexResult(indexed=0, reused=len(prepared))

    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(missing), batch_size):
        batch_cards = missing[start : start + batch_size]
        batch = provider.embed([item.embedding_text for item in batch_cards], "DOCUMENT")
        vectors.extend(_validate_batch(batch, profile, len(batch_cards)))

    with connection.begin():
        _insert_profile(connection, profile)
        for item, vector in zip(missing, vectors, strict=True):
            inserted = connection.execute(
                text("""
                    INSERT INTO knowledge.b_scenario_card_embedding (
                        card_id, card_version, profile_id, content_sha256, embedding
                    ) VALUES (
                        :card_id, :card_version, :profile_id, :content_sha256,
                        CAST(:embedding AS vector(1024))
                    )
                    ON CONFLICT (card_id, card_version, profile_id) DO NOTHING
                    RETURNING card_id
                """),
                {
                    "card_id": item.card.card_id,
                    "card_version": item.card.version,
                    "profile_id": profile.profile_id,
                    "content_sha256": item.content_sha256,
                    "embedding": _vector_text(vector),
                },
            ).scalar_one_or_none()
            if inserted is not None:
                indexed += 1
            stored_hash = connection.execute(
                text("""
                    SELECT content_sha256
                    FROM knowledge.b_scenario_card_embedding
                    WHERE card_id=:card_id AND card_version=:card_version
                      AND profile_id=:profile_id
                """),
                {
                    "card_id": item.card.card_id,
                    "card_version": item.card.version,
                    "profile_id": profile.profile_id,
                },
            ).scalar_one_or_none()
            if stored_hash != item.content_sha256:
                raise InputError("CARD_CATALOG_EMBEDDING_INTEGRITY_FAILED")
    return CardIndexResult(indexed=indexed, reused=len(prepared) - indexed)


def search_cards(
    engine: Engine,
    query_vector: Sequence[float],
    profile: EmbeddingProfile,
    limit: int = 50,
) -> list[CardCandidate]:
    """Return profile-matched cosine candidates; HNSW search is approximate."""
    _validate_profile(profile)
    vector = validate_embedding_vector(
        query_vector, profile.dimensions, normalized=profile.normalized
    )
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise InputError("CARD_CATALOG_SEARCH_LIMIT_INVALID")

    try:
        with engine.begin() as connection:
            profile_row = (
                connection.execute(
                    text("""
                        SELECT profile_id, provider, model, model_digest, dimensions,
                               query_instruction_version, normalized
                        FROM knowledge.b_embedding_profile
                        WHERE profile_id=:profile_id
                    """),
                    {"profile_id": profile.profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if profile_row is None:
                raise InputError("CARD_CATALOG_PROFILE_NOT_INDEXED")
            _check_profile_row(profile_row, profile)
            has_vectors = connection.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1 FROM knowledge.b_scenario_card_embedding
                        WHERE profile_id=:profile_id
                    )
                """),
                {"profile_id": profile.profile_id},
            ).scalar_one()
            if not has_vectors:
                raise InputError("CARD_CATALOG_PROFILE_NOT_INDEXED")

            # pgvector 0.8+ continues filtered HNSW scans until enough profile rows are found.
            connection.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
            rows = (
                connection.execute(
                    text("""
                        SELECT c.card_id, c.card_version,
                               c.content_sha256 AS card_sha256,
                               c.payload_size_bytes,
                               e.content_sha256 AS embedding_sha256,
                               e.embedding <=> CAST(:query_vector AS vector(1024)) AS distance
                        FROM knowledge.b_scenario_card_embedding AS e
                        JOIN knowledge.b_scenario_card AS c
                          ON c.card_id=e.card_id AND c.card_version=e.card_version
                         AND c.content_sha256=e.content_sha256
                        WHERE e.profile_id=:profile_id
                        ORDER BY e.embedding <=> CAST(:query_vector AS vector(1024)),
                                 c.card_id, c.card_version
                        LIMIT :limit
                    """),
                    {
                        "query_vector": _vector_text(vector),
                        "profile_id": profile.profile_id,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
            return _fetch_candidate_payloads(connection, rows, profile)
    except SQLAlchemyError:
        raise InputError("CARD_CATALOG_SEARCH_FAILED") from None


def _fetch_candidate_payloads(
    connection: Connection,
    candidates: Sequence[RowMapping],
    profile: EmbeddingProfile,
) -> list[CardCandidate]:
    total_size = 0
    for candidate in candidates:
        size = candidate["payload_size_bytes"]
        if type(size) is not int or size < 1:
            raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")
        total_size += size
        if total_size > MAX_FILE_BYTES:
            raise InputError("CARD_CATALOG_RESULT_TOO_LARGE")
    if not candidates:
        return []

    values_sql = ", ".join(
        f"(:candidate_id_{index}, :candidate_version_{index})" for index in range(len(candidates))
    )
    params: dict[str, str] = {"profile_id": profile.profile_id}
    for index, candidate in enumerate(candidates):
        params[f"candidate_id_{index}"] = candidate["card_id"]
        params[f"candidate_version_{index}"] = candidate["card_version"]
    payload_rows = (
        connection.execute(
            text(f"""
                WITH requested(card_id, card_version) AS (VALUES {values_sql})
                SELECT c.card_id, c.card_version,
                       c.content_sha256 AS card_sha256,
                       c.payload_size_bytes, c.payload_bytes,
                       e.content_sha256 AS embedding_sha256
                FROM requested AS wanted
                JOIN knowledge.b_scenario_card AS c
                  ON c.card_id=wanted.card_id AND c.card_version=wanted.card_version
                JOIN knowledge.b_scenario_card_embedding AS e
                  ON e.card_id=c.card_id AND e.card_version=c.card_version
                 AND e.content_sha256=c.content_sha256
                 AND e.profile_id=:profile_id
            """),
            params,
        )
        .mappings()
        .all()
    )
    by_identity = {(row["card_id"], row["card_version"]): row for row in payload_rows}
    if len(by_identity) != len(payload_rows) or len(by_identity) != len(candidates):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")

    results: list[CardCandidate] = []
    for candidate in candidates:
        key = (candidate["card_id"], candidate["card_version"])
        payload_row = by_identity.get(key)
        if (
            payload_row is None
            or payload_row["payload_size_bytes"] != candidate["payload_size_bytes"]
            or payload_row["card_sha256"] != candidate["card_sha256"]
            or payload_row["embedding_sha256"] != candidate["embedding_sha256"]
        ):
            raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")
        results.append(_candidate_from_row({**candidate, **payload_row}))
    return results


def _prepare_cards(cards: Sequence[ScenarioCard]) -> list[_PreparedCard]:
    unique: dict[tuple[str, str], _PreparedCard] = {}
    for card in cards:
        if not isinstance(card, ScenarioCard):
            raise InputError("CARD_CATALOG_CARD_INVALID")
        owned = card.model_copy(deep=True)
        try:
            payload = json_bytes(owned.model_dump(mode="json"))
            embedding_text = json_bytes(
                {
                    "claim_basis": owned.claim_basis,
                    "node_types": owned.node_types,
                    "required_artifact_kinds": owned.required_artifact_kinds,
                    "runtime_profile": owned.runtime_profile,
                    "summary": owned.summary,
                    "template_id": owned.template_id,
                    "template_version": owned.template_version,
                    "title": owned.title,
                }
            ).decode("utf-8")
        except (TypeError, UnicodeError, ValueError):
            raise InputError("CARD_CATALOG_CARD_INVALID") from None
        if not payload or len(payload) > MAX_FILE_BYTES:
            raise InputError("FILE_SIZE_LIMIT")
        if len(embedding_text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise InputError("EMBEDDING_INPUT_TOO_LARGE")
        key = (owned.card_id, owned.version)
        item = _PreparedCard(owned, payload, digest(payload), embedding_text)
        previous = unique.get(key)
        if previous is not None and previous.content_sha256 != item.content_sha256:
            raise InputError("KNOWLEDGE_CARD_ID_CONFLICT")
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _validate_profile(profile: EmbeddingProfile) -> None:
    validate_embedding_profile(profile)
    if profile.dimensions != SUPPORTED_DIMENSIONS:
        raise InputError("CARD_CATALOG_DIMENSIONS_UNSUPPORTED")


def _validate_batch(
    batch: EmbeddingBatch, expected_profile: EmbeddingProfile, expected_count: int
) -> list[tuple[float, ...]]:
    validated = validate_embedding_batch(
        batch,
        expected_count=expected_count,
        expected_profile=expected_profile,
        dimensions=expected_profile.dimensions,
    )
    return list(validated.vectors)


def _vector_text(vector: tuple[float, ...]) -> str:
    return json.dumps(vector, allow_nan=False, separators=(",", ":"))


@contextmanager
def _profile_lock(engine: Engine, profile: EmbeddingProfile) -> Iterator[Connection]:
    """Serialize one profile's catalog fill across provider calls and DB writes."""
    with engine.connect() as connection:
        try:
            acquired = connection.execute(
                text(_PROFILE_LOCK_SQL), {"profile_id": profile.profile_id}
            ).scalar_one()
            if not acquired:
                raise InputError("CARD_CATALOG_PROFILE_ALREADY_INDEXING")
            connection.commit()
            try:
                yield connection
            finally:
                try:
                    connection.execute(
                        text(_PROFILE_UNLOCK_SQL), {"profile_id": profile.profile_id}
                    )
                except SQLAlchemyError:
                    connection.invalidate()
        finally:
            if connection.in_transaction():
                connection.rollback()


def _check_stored_card(row: RowMapping, expected: _PreparedCard) -> None:
    if row["content_sha256"] != expected.content_sha256:
        raise InputError("KNOWLEDGE_CARD_ID_CONFLICT")
    try:
        payload = bytes(row["payload_bytes"])
    except (TypeError, ValueError):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED") from None
    if (
        row["payload_size_bytes"] != len(payload)
        or len(payload) != len(expected.payload)
        or payload != expected.payload
        or digest(payload) != expected.content_sha256
    ):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")


def _insert_profile(connection: Connection, profile: EmbeddingProfile) -> None:
    connection.execute(
        text("""
            INSERT INTO knowledge.b_embedding_profile (
                profile_id, provider, model, model_digest, dimensions,
                query_instruction_version, normalized
            ) VALUES (
                :profile_id, :provider, :model, :model_digest, :dimensions,
                :query_instruction_version, :normalized
            )
            ON CONFLICT (profile_id) DO NOTHING
        """),
        _profile_values(profile),
    )
    row = (
        connection.execute(
            text("""
                SELECT profile_id, provider, model, model_digest, dimensions,
                       query_instruction_version, normalized
                FROM knowledge.b_embedding_profile
                WHERE profile_id=:profile_id
            """),
            {"profile_id": profile.profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise InputError("CARD_CATALOG_PROFILE_STORE_FAILED")
    _check_profile_row(row, profile)


def _profile_values(profile: EmbeddingProfile) -> dict[str, str | int | bool]:
    return {
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "model": profile.model,
        "model_digest": profile.model_digest,
        "dimensions": profile.dimensions,
        "query_instruction_version": profile.query_instruction_version,
        "normalized": profile.normalized,
    }


def _check_profile_row(row: RowMapping, expected: EmbeddingProfile) -> None:
    actual = EmbeddingProfile(
        provider=row["provider"],
        model=row["model"],
        model_digest=row["model_digest"],
        dimensions=row["dimensions"],
        query_instruction_version=row["query_instruction_version"],
        normalized=row["normalized"],
    )
    _validate_profile(actual)
    if row["profile_id"] != expected.profile_id or actual != expected:
        raise InputError("CARD_CATALOG_PROFILE_INTEGRITY_FAILED")


def _candidate_from_row(row: Mapping[str, Any]) -> CardCandidate:
    try:
        payload = bytes(row["payload_bytes"])
        canonical_sha256 = str(row["card_sha256"])
        embedding_sha256 = str(row["embedding_sha256"])
        distance = float(row["distance"])
    except (TypeError, ValueError, OverflowError):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED") from None
    if (
        len(payload) != row["payload_size_bytes"]
        or len(payload) > MAX_FILE_BYTES
        or digest(payload) != canonical_sha256
        or embedding_sha256 != canonical_sha256
        or not math.isfinite(distance)
    ):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")
    try:
        card = parse_file_model(ScenarioCard, parse_json(payload))
        canonical = json_bytes(card.model_dump(mode="json"))
    except InputError:
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED") from None
    if (
        canonical != payload
        or card.card_id != row["card_id"]
        or card.version != row["card_version"]
    ):
        raise InputError("CARD_CATALOG_CONTENT_INTEGRITY_FAILED")
    score = 1.0 - distance
    if score < -1.000001 or score > 1.000001:
        raise InputError("CARD_CATALOG_SCORE_INVALID")
    return CardCandidate(
        card=card,
        content_sha256=canonical_sha256,
        score=min(1.0, max(-1.0, score)),
    )
