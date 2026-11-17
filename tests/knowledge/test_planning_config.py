import json

import pytest
from pydantic import ValidationError

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge import card_catalog, planner
from flowdelta.knowledge.planning_config import FrozenCard, PlanningConfig
from flowdelta.knowledge.retrieval import vector_query_context

from .test_planner import inputs
from .test_vector_planner import PROFILE, _Provider


def test_vector_configuration_requires_fixed_scope_and_profile():
    with pytest.raises(ValidationError):
        PlanningConfig(retrieval="VECTOR")
    card = FrozenCard(card_id="card", version="1", sha256="a" * 64)
    with pytest.raises(ValidationError):
        PlanningConfig(retrieval="VECTOR", frozen_cards=[card, card], embedding_profile_id="p")


def test_pinned_vector_plan_checks_provider_and_returned_card(tmp_path, monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    card = cards[0]
    pins = {(card.card_id, card.version): digest(json_bytes(card.model_dump(mode="json")))}

    def search(engine, vector, profile, limit, *, pinned_cards):
        assert pinned_cards == pins
        return [card_catalog.CardCandidate(card, next(iter(pins.values())), 0.8)]

    monkeypatch.setattr(card_catalog, "search_cards", search)
    plan = planner.write_vector_plan(
        handoff,
        analysis,
        catalog,
        tmp_path / "plan.json",
        engine=object(),
        provider=_Provider(),
        pinned_cards=pins,
        expected_profile_id=PROFILE.profile_id,
    )
    assert plan.retrieval_ref is not None
    result = json.loads((tmp_path / "retrieval.json").read_bytes())
    assert result["query"]["vector"]["search_scope"][0]["card_id"] == card.card_id
    (tmp_path / "new").mkdir()
    with pytest.raises(InputError, match="RETRIEVAL_PROFILE_MISMATCH"):
        planner.write_vector_plan(
            handoff,
            analysis,
            catalog,
            tmp_path / "new" / "plan.json",
            engine=object(),
            provider=_Provider(),
            pinned_cards=pins,
            expected_profile_id="changed",
        )


def test_query_uses_task_and_policy_but_not_case_or_analysis_identity():
    handoff, analysis, _, catalog = inputs()
    query = vector_query_context(handoff, analysis, catalog, planner.PRODUCER)
    changed = handoff.model_copy(deep=True)
    changed.case.case_id = "expected-regression-private-name"
    changed_analysis = analysis.model_copy(deep=True)
    changed_analysis.case_id = changed.case.case_id
    changed_analysis.analysis_id = "private-analysis"
    assert query == vector_query_context(changed, changed_analysis, catalog, planner.PRODUCER)
    changed.policy.rules[0].condition = "new policy condition"
    assert query != vector_query_context(changed, changed_analysis, catalog, planner.PRODUCER)
