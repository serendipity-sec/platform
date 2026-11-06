import json
import subprocess
import sys

import pytest

from flowdelta.ingestion import InputError, digest, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge import card_catalog, planner
from flowdelta.knowledge.card_catalog import CardCandidate
from flowdelta.knowledge.embedding import MAX_TEXT_BYTES, EmbeddingBatch, EmbeddingProfile
from flowdelta.pipeline_contracts import RetrievalResult
from flowdelta.pipeline_io import verify_ref

from .test_planner import inputs
from .test_static_planning import ROOT, planning_inputs

PROFILE = EmbeddingProfile(
    provider="test-provider",
    model="test-embedding",
    model_digest="sha256:" + "a" * 64,
    dimensions=1024,
    query_instruction_version="test-query-v1",
    normalized=True,
)


class _Provider:
    def __init__(self):
        self.calls = []

    def embed(self, texts, purpose):
        self.calls.append((texts, purpose))
        return EmbeddingBatch(PROFILE, ((1.0, *([0.0] * 1023)),))


def _install_search(monkeypatch, cards, scores):
    expected_cards = sorted(cards, key=lambda card: (card.card_id, card.version))

    def search(engine, query_vector, profile, limit):
        assert profile == PROFILE
        return [
            CardCandidate(
                card=card,
                content_sha256=digest(json_bytes(card.model_dump(mode="json"))),
                score=score,
            )
            for card, score in zip(expected_cards, scores, strict=True)
        ]

    monkeypatch.setattr(card_catalog, "search_cards", search)


def test_write_vector_plan_embeds_context_and_publishes_linked_outputs(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    engine = object()
    provider = _Provider()
    expected_cards = sorted(cards, key=lambda card: (card.card_id, card.version))

    def search(search_engine, query_vector, profile, limit):
        assert search_engine is engine
        assert tuple(query_vector) == (1.0, *([0.0] * 1023))
        assert profile == PROFILE
        assert limit == 20
        return [
            CardCandidate(
                card=card,
                content_sha256=digest(json_bytes(card.model_dump(mode="json"))),
                score=score,
            )
            for card, score in zip(expected_cards, (0.75, 0.25), strict=True)
        ]

    monkeypatch.setattr(card_catalog, "search_cards", search)
    plan = planner.write_vector_plan(
        handoff,
        analysis,
        catalog,
        tmp_path / "plan.json",
        engine=engine,
        provider=provider,
        limit=20,
    )

    assert len(provider.calls) == 1
    [query_texts, purpose] = provider.calls[0]
    assert purpose == "QUERY"
    assert len(query_texts) == 1
    assert analysis.analysis_id in query_texts[0]
    assert "impact_paths" in query_texts[0]

    retrieval = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )
    verify_ref(tmp_path, plan.retrieval_ref)
    verify_ref(tmp_path, retrieval.kb_snapshot_ref)
    assert retrieval.method == "VECTOR"
    assert retrieval.model_version == PROFILE.model_digest
    assert retrieval.query["vector"]["profile"]["profile_id"] == PROFILE.profile_id
    assert retrieval.query["vector"]["query_sha256"] == digest(query_texts[0].encode("utf-8"))
    assert [item["rank"] for item in retrieval.query["vector"]["candidates"]] == [1, 2]
    assert [hit.score for hit in retrieval.hits] == [0.75, 0.25]
    assert plan.plan_id == f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}"
    snapshot = json.loads((tmp_path / retrieval.kb_snapshot_ref.path).read_bytes())
    assert [item["card"]["card_id"] for item in snapshot["cards"]] == [
        card.card_id for card in expected_cards
    ]


def test_offline_planner_import_does_not_require_database_extra():
    code = """
import builtins
original_import = builtins.__import__
def reject_sqlalchemy(name, *args, **kwargs):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError("SQLAlchemy blocked", name="sqlalchemy")
    return original_import(name, *args, **kwargs)
builtins.__import__ = reject_sqlalchemy
import flowdelta.knowledge.planner
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_vector_static_plan_binds_selection_and_preserves_static_contract(tmp_path, monkeypatch):
    from flowdelta.graph.analyze import identified
    from flowdelta.runtime.static_scan import validate_static_plan

    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.impact_paths.append(
        analysis.impact_paths[0].model_copy(update={"path_id": "impact-also"})
    )
    analysis = identified(analysis)
    _install_search(monkeypatch, cards, [0.75] * len(cards))
    plans = []
    for label, selected in [
        ("unselected", None),
        ("first", "impact-memory-delete"),
        ("second", "impact-also"),
    ]:
        output = tmp_path / label / "plan.json"
        output.parent.mkdir()
        plan = planner.write_vector_plan(
            handoff,
            analysis,
            catalog,
            output,
            engine=object(),
            provider=_Provider(),
            handoff_root=handoff_root,
            repo_root=ROOT,
            selected_path_id=selected,
        )
        validate_static_plan(plan, catalog, ROOT)
        assert plan.planner_metadata["selected_path_id"] == selected
        assert plan.planner_metadata["security_outcome"] is None
        plans.append(plan)
    assert len({plan.plan_id for plan in plans}) == 3
    assert plans[0].planner_metadata["path_selection"]["status"] == "AMBIGUOUS"
    retrieval = parse_file_model(
        RetrievalResult, parse_json((tmp_path / "first" / plans[1].retrieval_ref.path).read_bytes())
    )
    with pytest.raises(InputError, match="RETRIEVAL_ID_MISMATCH"):
        planner.compose_plan(
            handoff,
            analysis,
            cards,
            catalog,
            retrieval,
            handoff_root=handoff_root,
            repo_root=ROOT,
            selected_path_id="impact-also",
        )


def test_different_change_values_produce_different_vector_queries_and_plan_ids(
    tmp_path, monkeypatch
):
    handoff, analysis, cards, catalog = inputs()
    _install_search(monkeypatch, cards, (0.75, 0.25))
    before = analysis.model_copy(deep=True)
    after = analysis.model_copy(deep=True)
    before.changes[0].before_value = "input-v1"
    before.changes[0].after_value = "input-v2"
    after.changes[0].before_value = "input-v1"
    after.changes[0].after_value = "input-v3"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    provider = _Provider()

    first = planner.write_vector_plan(
        handoff, before, catalog, first_dir / "plan.json", engine=object(), provider=provider
    )
    second = planner.write_vector_plan(
        handoff, after, catalog, second_dir / "plan.json", engine=object(), provider=provider
    )

    assert provider.calls[0][0] != provider.calls[1][0]
    assert "input-v2" in provider.calls[0][0][0]
    assert "input-v3" in provider.calls[1][0][0]
    assert first.plan_id != second.plan_id


def test_vector_rejects_tampered_profile_candidate_hash_and_score(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    _install_search(monkeypatch, cards, (0.75, 0.25))
    plan = planner.write_vector_plan(
        handoff, analysis, catalog, tmp_path / "plan.json", engine=object(), provider=_Provider()
    )
    retrieval = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )

    tampered = retrieval.model_copy(deep=True)
    tampered.query["vector"]["profile"]["model_digest"] = "sha256:" + "b" * 64
    with pytest.raises(InputError, match="RETRIEVAL_PROFILE_MISMATCH"):
        planner.compose_plan(handoff, analysis, cards, catalog, tampered)

    tampered = retrieval.model_copy(deep=True)
    tampered.query["vector"]["candidates"][0]["card_sha256"] = "0" * 64
    with pytest.raises(InputError, match="RETRIEVAL_CANDIDATE_MISMATCH"):
        planner.compose_plan(handoff, analysis, cards, catalog, tampered)

    tampered = retrieval.model_copy(deep=True)
    tampered.query["vector"]["candidates"][0]["similarity"] = 0.5
    with pytest.raises(InputError, match="RETRIEVAL_CONTENT_MISMATCH"):
        planner.compose_plan(handoff, analysis, cards, catalog, tampered)

    tampered = retrieval.model_copy(deep=True)
    tampered.query["vector"]["candidates"][0]["similarity"] = 10**400
    with pytest.raises(InputError, match="RETRIEVAL_SCORE_INVALID"):
        planner.compose_plan(handoff, analysis, cards, catalog, tampered)


def test_unregistered_template_is_preserved_as_excluded_candidate(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    unknown = cards[0].model_copy(update={"template_id": "unregistered-template"})
    candidates = [unknown, cards[1]]
    _install_search(monkeypatch, candidates, (0.9, 0.7))
    plan = planner.write_vector_plan(
        handoff, analysis, catalog, tmp_path / "plan.json", engine=object(), provider=_Provider()
    )
    retrieval = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )

    assert retrieval.excluded_reasons == {
        f"{unknown.card_id}@{unknown.version}": "TEMPLATE_NOT_REGISTERED"
    }
    assert [item["card_id"] for item in retrieval.query["vector"]["candidates"]] == [
        unknown.card_id,
        cards[1].card_id,
    ]
    assert [hit.card_id for hit in retrieval.hits] == [cards[1].card_id]


def test_incompatible_card_artifacts_are_excluded(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    incompatible = cards[0].model_copy(update={"required_artifact_kinds": ["missing-kind"]})
    candidates = [incompatible, cards[1]]
    _install_search(monkeypatch, candidates, (0.9, 0.7))
    plan = planner.write_vector_plan(
        handoff, analysis, catalog, tmp_path / "plan.json", engine=object(), provider=_Provider()
    )
    retrieval = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )

    assert retrieval.excluded_reasons == {
        f"{incompatible.card_id}@{incompatible.version}": "TEMPLATE_ARTIFACTS_INCOMPATIBLE"
    }
    assert [hit.card_id for hit in retrieval.hits] == [cards[1].card_id]


def test_missing_profile_fails_without_keyword_fallback(tmp_path, monkeypatch):
    handoff, analysis, _, catalog = inputs()
    provider = _Provider()

    def missing_profile(engine, query_vector, profile, limit):
        raise InputError("CARD_CATALOG_PROFILE_NOT_INDEXED")

    monkeypatch.setattr(card_catalog, "search_cards", missing_profile)
    with pytest.raises(InputError, match="CARD_CATALOG_PROFILE_NOT_INDEXED"):
        planner.write_vector_plan(
            handoff,
            analysis,
            catalog,
            tmp_path / "plan.json",
            engine=object(),
            provider=provider,
        )
    assert provider.calls[0][1] == "QUERY"
    assert not (tmp_path / "plan.json").exists()


def test_embedding_query_redacts_credential_and_sensitive_parameter_values(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    analysis.changes[0].before_value = {"password": "old-password-value", "label": "old"}
    analysis.changes[0].after_value = {"password": "new-password-value", "label": "new"}
    analysis.changes[0].before_source.json_pointer = "/parameters/config"
    analysis.changes[0].after_source.json_pointer = "/parameters/config"
    analysis.changes.append(
        analysis.changes[0].model_copy(
            deep=True,
            update={
                "change_id": "change-secret-credential",
                "kind": "CREDENTIAL_CHANGED",
                "before_value": "old-credential-value",
                "after_value": "new-credential-value",
            },
        )
    )
    private_change = analysis.changes[0].model_copy(
        deep=True,
        update={
            "change_id": "change-private-field",
            "before_value": "old-private-key-value",
            "after_value": "new-private-key-value",
        },
    )
    private_change.before_source.json_pointer = "/parameters/privateKey"
    private_change.after_source.json_pointer = "/parameters/privateKey"
    analysis.changes.append(private_change)
    _install_search(monkeypatch, cards, (0.75, 0.25))
    provider = _Provider()
    planner.write_vector_plan(
        handoff, analysis, catalog, tmp_path / "plan.json", engine=object(), provider=provider
    )
    query = provider.calls[0][0][0]

    assert "old-password-value" not in query
    assert "new-password-value" not in query
    assert "old-credential-value" not in query
    assert "new-credential-value" not in query
    assert "old-private-key-value" not in query
    assert "new-private-key-value" not in query
    assert "SENSITIVE_OBJECT_FIELD" in query
    assert "SENSITIVE_SOURCE_FIELD" in query
    assert "CREDENTIAL_CHANGE" in query
    assert '"label": "old"' in query
    assert '"label": "new"' in query


def test_oversized_vector_query_fails_before_provider_call(tmp_path):
    handoff, analysis, _, catalog = inputs()
    analysis.changes[0].after_value = "x" * MAX_TEXT_BYTES
    provider = _Provider()
    with pytest.raises(InputError, match="VECTOR_QUERY_TOO_LARGE"):
        planner.write_vector_plan(
            handoff,
            analysis,
            catalog,
            tmp_path / "plan.json",
            engine=object(),
            provider=provider,
        )
    assert provider.calls == []
