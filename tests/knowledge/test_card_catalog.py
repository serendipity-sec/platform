"""Shared immutable ScenarioCard catalog and pgvector search tests."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text

import flowdelta.knowledge.card_catalog as catalog
from flowdelta.ingestion import MAX_FILE_BYTES, InputError, digest, json_bytes
from flowdelta.knowledge.card_catalog import CardIndexResult
from flowdelta.knowledge.database import engine_for, migrate
from flowdelta.knowledge.embedding import EmbeddingBatch, EmbeddingProfile, EmbeddingPurpose
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.pipeline_contracts import ScenarioCard

ROOT = Path(__file__).resolve().parents[2]
CARD_PATH = ROOT / "examples/contracts/order-lookup/cards/normal-order-response.json"
_DIGEST = "sha256:" + "a" * 64


def _profile(**changes: object) -> EmbeddingProfile:
    profile = EmbeddingProfile(
        provider="test-provider",
        model="test-model-v1",
        model_digest=_DIGEST,
        dimensions=1024,
        query_instruction_version="test-query-v1",
        normalized=True,
    )
    return replace(profile, **changes)


def _card(*, card_id: str | None = None, title: str | None = None) -> ScenarioCard:
    card = ScenarioCard.model_validate_json(CARD_PATH.read_bytes())
    values = card.model_dump(mode="json")
    values["card_id"] = card_id or f"catalog-test-{uuid.uuid4().hex}"
    if title is not None:
        values["title"] = title
    return ScenarioCard.model_validate(values)


class _Provider:
    def __init__(
        self,
        profile: EmbeddingProfile,
        *,
        returned_profile: EmbeddingProfile | None = None,
        vectors: tuple[tuple[float, ...], ...] | None = None,
    ) -> None:
        self.profile = profile
        self.returned_profile = returned_profile or profile
        self.vectors = vectors
        self.calls: list[tuple[list[str], EmbeddingPurpose]] = []

    def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> EmbeddingBatch:
        self.calls.append((texts, purpose))
        vectors = (
            self.vectors
            if self.vectors is not None
            else tuple((1.0, *([0.0] * 1023)) for _ in texts)
        )
        return EmbeddingBatch(self.returned_profile, vectors)


class _Result:
    def __init__(self, *, row=None, value=None, rows=None) -> None:
        self.row = row
        self.value = value
        self.rows = rows or []

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def all(self):
        return self.rows


class _MemoryCatalogEngine:
    """Small DB boundary fake for exercising reuse and conflicts without PostgreSQL."""

    def __init__(self) -> None:
        self.cards: dict[tuple[str, str], dict[str, object]] = {}
        self.profiles: dict[str, dict[str, object]] = {}
        self.embeddings: dict[tuple[str, str, str], dict[str, object]] = {}
        self.profile_lock_acquired = True
        self.payload_fetch_queries = 0
        self.payload_fetch_ids: list[tuple[str, str]] = []

    @contextmanager
    def connect(self):
        yield _MemoryCatalogConnection(self)

    @contextmanager
    def begin(self):
        with self.connect() as connection:
            yield connection


class _MemoryCatalogConnection:
    def __init__(self, engine: _MemoryCatalogEngine) -> None:
        self.engine = engine

    @contextmanager
    def begin(self):
        yield self

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def in_transaction(self) -> bool:
        return False

    def invalidate(self) -> None:
        pass

    def execute(self, statement, params=None):
        sql = str(statement)
        values = params or {}
        if "pg_try_advisory_lock" in sql or "pg_advisory_unlock" in sql:
            return _Result(value=self.engine.profile_lock_acquired)
        if sql.startswith("SET LOCAL"):
            return _Result()
        if "FROM knowledge.b_embedding_profile" in sql:
            return _Result(row=self.engine.profiles.get(values["profile_id"]))
        if "INSERT INTO knowledge.b_scenario_card (" in sql:
            key = (values["card_id"], values["card_version"])
            self.engine.cards.setdefault(key, dict(values))
            return _Result()
        if "FROM knowledge.b_scenario_card\n" in sql:
            key = (values["card_id"], values["card_version"])
            return _Result(row=self.engine.cards.get(key))
        if "knowledge.b_scenario_card_embedding AS e" in sql:
            if "c.payload_bytes" in sql:
                self.engine.payload_fetch_queries += 1
                rows = []
                candidate_count = sum(key.startswith("candidate_id_") for key in values)
                requested = [
                    (values[f"candidate_id_{index}"], values[f"candidate_version_{index}"])
                    for index in range(candidate_count)
                ]
                self.engine.payload_fetch_ids.extend(requested)
                for card_id, version in requested:
                    embedding = self.engine.embeddings.get((card_id, version, values["profile_id"]))
                    card = self.engine.cards[(card_id, version)]
                    if embedding is None:
                        continue
                    rows.append(
                        {
                            "card_id": card_id,
                            "card_version": version,
                            "card_sha256": card["content_sha256"],
                            "payload_size_bytes": card["payload_size_bytes"],
                            "payload_bytes": card["payload_bytes"],
                            "embedding_sha256": embedding["content_sha256"],
                            "distance": 0.0,
                        }
                    )
                return _Result(rows=rows)
            rows = []
            for (card_id, version, profile_id), embedding in self.engine.embeddings.items():
                if profile_id != values["profile_id"]:
                    continue
                card = self.engine.cards[(card_id, version)]
                rows.append(
                    {
                        "card_id": card_id,
                        "card_version": version,
                        "card_sha256": card["content_sha256"],
                        "payload_size_bytes": card["payload_size_bytes"],
                        "embedding_sha256": embedding["content_sha256"],
                        "distance": 0.0,
                    }
                )
            rows.sort(key=lambda row: (row["distance"], row["card_id"], row["card_version"]))
            return _Result(rows=rows[: values["limit"]])
        if "INSERT INTO knowledge.b_embedding_profile (" in sql:
            self.engine.profiles.setdefault(values["profile_id"], dict(values))
            return _Result()
        if "INSERT INTO knowledge.b_scenario_card_embedding (" in sql:
            key = (values["card_id"], values["card_version"], values["profile_id"])
            if key in self.engine.embeddings:
                return _Result(value=None)
            self.engine.embeddings[key] = dict(values)
            return _Result(value=values["card_id"])
        if "FROM knowledge.b_scenario_card_embedding" in sql:
            if "SELECT EXISTS" in sql:
                exists = any(key[2] == values["profile_id"] for key in self.engine.embeddings)
                return _Result(value=exists)
            key = (values["card_id"], values["card_version"], values["profile_id"])
            row = self.engine.embeddings.get(key)
            if row is None:
                return _Result(value=None)
            return _Result(value=row["content_sha256"])
        raise AssertionError(f"unhandled SQL in catalog fake: {sql}")


def test_unsupported_dimension_is_rejected_before_database_or_provider_access():
    class ForbiddenEngine:
        def begin(self):
            raise AssertionError("unsupported profile must fail before opening a transaction")

    provider = _Provider(_profile(dimensions=768))
    with pytest.raises(InputError, match="CARD_CATALOG_DIMENSIONS_UNSUPPORTED"):
        catalog.index_cards(ForbiddenEngine(), [_card()], provider, provider.profile)
    assert provider.calls == []


def test_conflicting_duplicate_identity_is_rejected_before_database_access():
    class ForbiddenEngine:
        def begin(self):
            raise AssertionError("conflicting duplicate identity must fail before persistence")

    first = _card(card_id="catalog-duplicate", title="First title")
    second = _card(card_id="catalog-duplicate", title="Changed title")
    provider = _Provider(_profile())
    with pytest.raises(InputError, match="KNOWLEDGE_CARD_ID_CONFLICT"):
        catalog.index_cards(ForbiddenEngine(), [first, second], provider, provider.profile)
    assert provider.calls == []


def test_memory_db_boundary_reuses_document_vectors_and_rejects_identity_conflicts():
    engine = _MemoryCatalogEngine()
    profile = _profile()
    provider = _Provider(profile)
    cards = [_card(), _card()]

    result = catalog.index_cards(engine, cards, provider, profile)
    assert result == CardIndexResult(indexed=2, reused=0)
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == "DOCUMENT"

    result = catalog.index_cards(engine, cards, provider, profile)
    assert result == CardIndexResult(indexed=0, reused=2)
    assert len(provider.calls) == 1

    conflicting = _card(card_id=cards[0].card_id, title="Changed immutable content")
    with pytest.raises(InputError, match="KNOWLEDGE_CARD_ID_CONFLICT"):
        catalog.index_cards(engine, [conflicting], provider, profile)
    assert len(provider.calls) == 1

    other_profile = replace(profile, model_digest="sha256:" + "c" * 64)
    other_provider = _Provider(other_profile)
    result = catalog.index_cards(engine, cards, other_provider, other_profile)
    assert result == CardIndexResult(indexed=2, reused=0)
    assert len(other_provider.calls) == 1


def test_memory_db_boundary_honors_smaller_document_batch_size():
    engine = _MemoryCatalogEngine()
    profile = _profile()
    provider = _Provider(profile)
    cards = [_card() for _ in range(9)]

    result = catalog.index_cards(engine, cards, provider, profile, batch_size=4)

    assert result == CardIndexResult(indexed=9, reused=0)
    assert [len(texts) for texts, purpose in provider.calls] == [4, 4, 1]
    assert all(purpose == "DOCUMENT" for _, purpose in provider.calls)


def test_busy_profile_lock_prevents_duplicate_document_embedding():
    engine = _MemoryCatalogEngine()
    engine.profile_lock_acquired = False
    profile = _profile()
    provider = _Provider(profile)
    with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_ALREADY_INDEXING"):
        catalog.index_cards(engine, [_card()], provider, profile)
    assert provider.calls == []
    assert engine.cards == {}


def test_search_rejects_unknown_or_mismatched_profile_metadata():
    engine = _MemoryCatalogEngine()
    profile = _profile()
    with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_NOT_INDEXED"):
        catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile)

    provider = _Provider(profile)
    catalog.index_cards(engine, [_card()], provider, profile)
    engine.profiles[profile.profile_id]["model"] = "tampered-model"
    with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_INTEGRITY_FAILED"):
        catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile)


def test_search_checks_aggregate_size_before_fetching_card_payloads():
    engine = _MemoryCatalogEngine()
    profile = _profile()
    engine.profiles[profile.profile_id] = {
        "profile_id": profile.profile_id,
        "provider": profile.provider,
        "model": profile.model,
        "model_digest": profile.model_digest,
        "dimensions": profile.dimensions,
        "query_instruction_version": profile.query_instruction_version,
        "normalized": profile.normalized,
    }
    payload_size = MAX_FILE_BYTES // 2 + 1
    for card_id in ("oversized-a", "oversized-b"):
        sha256 = digest(card_id.encode())
        card = _card(card_id=card_id)
        engine.cards[(card_id, card.version)] = {
            "content_sha256": sha256,
            "payload_size_bytes": payload_size,
            "payload_bytes": b"not fetched",
        }
        engine.embeddings[(card_id, card.version, profile.profile_id)] = {
            "content_sha256": sha256,
            "embedding": "[1.0]",
        }

    with pytest.raises(InputError, match="CARD_CATALOG_RESULT_TOO_LARGE"):
        catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile, limit=2)
    assert engine.payload_fetch_queries == 0


def test_search_fetches_exact_bounded_candidates_after_size_check():
    engine = _MemoryCatalogEngine()
    profile = _profile()
    provider = _Provider(profile)
    cards = [
        _card(card_id="search-bound-b"),
        _card(card_id="search-bound-a"),
    ]
    catalog.index_cards(engine, cards, provider, profile)

    candidates = catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile, limit=1)

    assert [candidate.card.card_id for candidate in candidates] == ["search-bound-a"]
    assert candidates[0].card == cards[1]
    assert candidates[0].content_sha256 == digest(json_bytes(cards[1].model_dump(mode="json")))
    assert candidates[0].score == pytest.approx(1.0)
    assert engine.payload_fetch_queries == 1
    assert engine.payload_fetch_ids == [(cards[1].card_id, cards[1].version)]


@pytest.mark.parametrize(
    ("returned_profile", "vectors", "error"),
    [
        ("mismatch", ((1.0, *([0.0] * 1023)),), "EMBEDDING_PROFILE_MISMATCH"),
        ("expected", (), "EMBEDDING_VECTOR_COUNT_INVALID"),
        ("expected", ((1.0, 0.0),), "EMBEDDING_DIMENSION_MISMATCH"),
        ("expected", ((float("inf"), *([0.0] * 1023)),), "EMBEDDING_VECTOR_INVALID"),
        ("expected", ((0.0,) * 1024,), "EMBEDDING_VECTOR_INVALID"),
        ("expected", ((2.0, *([0.0] * 1023)),), "EMBEDDING_VECTOR_NORMALIZATION_INVALID"),
    ],
)
def test_memory_db_boundary_rejects_invalid_batches_before_vector_insert(
    returned_profile, vectors, error
):
    engine = _MemoryCatalogEngine()
    profile = _profile()
    actual_profile = (
        _profile(model="different-model") if returned_profile == "mismatch" else profile
    )
    provider = _Provider(profile, returned_profile=actual_profile, vectors=vectors)
    with pytest.raises(InputError, match=error):
        catalog.index_cards(engine, [_card()], provider, profile)
    assert len(provider.calls) == 1
    assert engine.profiles == {}
    assert engine.embeddings == {}


@pytest.mark.skipif(
    not os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable PostgreSQL database",
)
def test_postgres_card_catalog_reuse_conflicts_profiles_and_search(tmp_path):
    settings = KnowledgeSettings(
        database_url=os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"],
        migration_url=os.environ.get(
            "FLOWDELTA_KB_TEST_MIGRATION_URL", os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"]
        ),
        raw_root=tmp_path,
    )
    migrate(settings)
    engine = engine_for(settings)
    try:
        profile = _profile(model=f"test-model-{uuid.uuid4().hex}")
        provider = _Provider(profile)
        cards = [
            _card(card_id=f"catalog-sort-b-{uuid.uuid4().hex}"),
            _card(card_id=f"catalog-sort-a-{uuid.uuid4().hex}"),
        ]
        result = catalog.index_cards(engine, cards, provider, profile)
        assert (result.indexed, result.reused) == (2, 0)
        assert len(provider.calls) == 1
        assert provider.calls[0][1] == "DOCUMENT"

        result = catalog.index_cards(engine, cards, provider, profile)
        assert (result.indexed, result.reused) == (0, 2)
        assert len(provider.calls) == 1, "an unchanged card/profile must not be re-embedded"

        candidates = catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile, limit=10)
        assert [item.card.card_id for item in candidates] == sorted(card.card_id for card in cards)
        assert all(item.score == pytest.approx(1.0) for item in candidates)
        for candidate in candidates:
            expected_card = next(card for card in cards if card.card_id == candidate.card.card_id)
            expected_bytes = json_bytes(expected_card.model_dump(mode="json"))
            assert candidate.card == expected_card
            assert candidate.content_sha256 == digest(expected_bytes)
            with engine.connect() as connection:
                stored = (
                    connection.execute(
                        text("""
                            SELECT content_sha256, payload_size_bytes, payload_bytes
                            FROM knowledge.b_scenario_card
                            WHERE card_id=:card_id AND card_version=:card_version
                        """),
                        {"card_id": expected_card.card_id, "card_version": expected_card.version},
                    )
                    .mappings()
                    .one()
                )
            assert bytes(stored["payload_bytes"]) == expected_bytes
            assert stored["payload_size_bytes"] == len(expected_bytes)
            assert stored["content_sha256"] == digest(expected_bytes)

        changed_profile = replace(profile, model_digest="sha256:" + "b" * 64)
        changed_provider = _Provider(changed_profile)
        result = catalog.index_cards(engine, cards, changed_provider, changed_profile)
        assert (result.indexed, result.reused) == (2, 0)
        assert len(changed_provider.calls) == 1
        assert len(catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile)) == 2
        assert len(catalog.search_cards(engine, (1.0, *([0.0] * 1023)), changed_profile)) == 2

        changed_card = _card(card_id=cards[0].card_id, title="Changed content")
        with pytest.raises(InputError, match="KNOWLEDGE_CARD_ID_CONFLICT"):
            catalog.index_cards(engine, [changed_card], provider, profile)
        assert len(provider.calls) == 1

        unknown_profile = replace(profile, model="not-indexed")
        with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_NOT_INDEXED"):
            catalog.search_cards(engine, (1.0, *([0.0] * 1023)), unknown_profile)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable PostgreSQL database",
)
@pytest.mark.parametrize(
    ("returned_profile", "vectors", "error"),
    [
        ("mismatch", ((1.0, *([0.0] * 1023)),), "EMBEDDING_PROFILE_MISMATCH"),
        ("expected", (), "EMBEDDING_VECTOR_COUNT_INVALID"),
        ("expected", ((1.0, 0.0),), "EMBEDDING_DIMENSION_MISMATCH"),
        ("expected", ((float("inf"), *([0.0] * 1023)),), "EMBEDDING_VECTOR_INVALID"),
        ("expected", ((0.0,) * 1024,), "EMBEDDING_VECTOR_INVALID"),
    ],
)
def test_postgres_rejects_invalid_document_embedding_batches_before_vector_insert(
    tmp_path, returned_profile, vectors, error
):
    settings = KnowledgeSettings(
        database_url=os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"],
        migration_url=os.environ.get(
            "FLOWDELTA_KB_TEST_MIGRATION_URL", os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"]
        ),
        raw_root=tmp_path,
    )
    migrate(settings)
    engine = engine_for(settings)
    try:
        profile = _profile(model=f"test-model-{uuid.uuid4().hex}")
        actual_profile = (
            _profile(model="different-model") if returned_profile == "mismatch" else profile
        )
        provider = _Provider(profile, returned_profile=actual_profile, vectors=vectors)
        card = _card()
        with pytest.raises(InputError, match=error):
            catalog.index_cards(engine, [card], provider, profile)

        with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_NOT_INDEXED"):
            catalog.search_cards(engine, (1.0, *([0.0] * 1023)), profile)
    finally:
        engine.dispose()
