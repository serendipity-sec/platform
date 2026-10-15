import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge import make_plan, planner, retrieve, write_plan

from .test_planner import inputs


def test_fallback_skips_incompatible_template():
    handoff, analysis, _, catalog = inputs()
    incompatible = catalog.templates[0].model_copy(deep=True)
    incompatible.template_id = "aaa-incompatible"
    incompatible.input_fields[0].pattern = "^OTHER-[0-9]{4}$"
    catalog.templates.insert(0, incompatible)
    assert make_plan(handoff, analysis, [], catalog).template_id == "normal-order-lookup"


def test_ranked_card_with_invalid_input_is_excluded():
    handoff, analysis, cards, catalog = inputs()
    incompatible = catalog.templates[0].model_copy(deep=True)
    incompatible.template_id = "aaa-incompatible"
    incompatible.input_fields[0].pattern = "^OTHER-[0-9]{4}$"
    catalog.templates.insert(0, incompatible)
    card = cards[0].model_copy(
        update={"card_id": "aaa-card", "template_id": incompatible.template_id}
    )
    cards.insert(0, card)
    result = retrieve(handoff, analysis, cards, catalog)
    assert result.excluded_reasons[f"{card.card_id}@{card.version}"] == "TEMPLATE_INPUT_INVALID"
    assert make_plan(handoff, analysis, cards, catalog).template_id == "normal-order-lookup"


def test_oversized_knowledge_has_stable_error_and_no_partial_output(tmp_path):
    handoff, analysis, cards, catalog = inputs()
    cards = [
        cards[0].model_copy(
            update={"card_id": f"large-{i}", "extensions": {"flowdelta.padding": "x" * 3_000_000}}
        )
        for i in range(3)
    ]
    with pytest.raises(InputError, match="KNOWLEDGE_SNAPSHOT_SIZE_LIMIT"):
        write_plan(handoff, analysis, cards, catalog, tmp_path / "plan.json")
    assert not list(tmp_path.iterdir())


def test_producer_version_changes_result_identity(monkeypatch):
    handoff, analysis, cards, catalog = inputs()
    first = make_plan(handoff, analysis, cards, catalog)
    monkeypatch.setattr(planner, "PRODUCER", "flowdelta.knowledge/2.0.0")
    second = make_plan(handoff, analysis, cards, catalog)
    assert first.plan_id != second.plan_id
    assert second == make_plan(handoff, analysis, cards, catalog)
