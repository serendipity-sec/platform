import json
from pathlib import Path

import pytest

from flowdelta.graph import analyze
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.knowledge.cards import load_cards
from flowdelta.knowledge.generation import GenerationResult, Proposal
from flowdelta.knowledge.order_plans import registered_candidates, write_order_plans
from flowdelta.knowledge.planning_config import GenerationSettings, PlanningConfig, SelectionBudget
from flowdelta.knowledge.repository import export_b_outputs, read_b_outputs
from flowdelta.knowledge.selection import select_trials
from flowdelta.order_evaluation import materialize_pair
from flowdelta.order_scenarios import ROOT, read_registered
from flowdelta.pipeline_contracts import TemplateCatalog

REPO = Path(__file__).resolve().parents[2]


def selection_inputs(tmp_path):
    family = json.loads(read_registered(REPO, f"{ROOT}/family.json"))
    materialize_pair(REPO, family["pairs"][2], tmp_path / "case")
    handoff = build_handoff(load_case(tmp_path / "case"))
    catalog = TemplateCatalog.model_validate_json(read_registered(REPO, f"{ROOT}/catalog.json"))
    return handoff, analyze(handoff), catalog


def config(**kwargs):
    return PlanningConfig(
        generation=GenerationSettings(
            provider="ollama", base_url="http://localhost:11434", model="test-only-model"
        ),
        **kwargs,
    )


class ChoiceProvider:
    def __init__(self, preferred="claimed-customer", *, mutate=None):
        self.preferred = preferred
        self.mutate = mutate
        self.calls = []

    def propose(self, context):
        self.calls.append(context)
        choices = []
        for candidate_id in (
            self.preferred,
            "claimed-tenant" if self.preferred == "claimed-customer" else "claimed-customer",
        ):
            choice = {
                "candidate_id": candidate_id,
                "card_keys": [f"order-{candidate_id}@1.0.0"],
                "path_ids": [],
                "reason": "Relevant changed identity field and card evidence",
            }
            choices.append(choice)
        if self.mutate:
            self.mutate(choices)
        return GenerationResult(
            Proposal.model_validate({"choices": choices}),
            {"called": True, "provider": "DETERMINISTIC_TEST_DOUBLE"},
        )


def test_model_priority_changes_actual_plans_without_dropping_mandatory(tmp_path):
    handoff, analysis, catalog = selection_inputs(tmp_path)
    for preferred in ("claimed-customer", "claimed-tenant"):
        output = tmp_path / preferred
        output.mkdir()
        provider = ChoiceProvider(preferred)
        plans = write_order_plans(
            handoff,
            analysis,
            catalog,
            repo=REPO,
            output=output,
            planning_config=config(),
            provider=provider,
        )
        assert [scenario for scenario, _, _ in plans] == [
            "normal",
            "other-order",
            "claimed-owner",
            preferred,
        ]
        assert len(provider.calls) == 1
        assert handoff.case.case_id not in json.dumps(provider.calls[0])
        assert analysis.analysis_id not in json.dumps(provider.calls[0])
        for _, plan, sources in plans:
            stored = read_b_outputs(sources["plan.json"])
            assert stored.plan == plan
            exported = output / plan.plan_id
            export_b_outputs(stored, exported)
            assert read_b_outputs(exported / "plan.json") == stored
            if stored.execution_inputs:
                assert (exported / "scenario.json").read_bytes() == sources[
                    "scenario.json"
                ].read_bytes()
            assert plan.planner_metadata["selection"]["reserved_c_runs"] == 8
        changed = json.loads(plans[-1][2]["plan.json"].read_bytes())
        changed["timeout_seconds"] -= 1
        plans[-1][2]["plan.json"].write_bytes(json_bytes(changed))
        with pytest.raises(InputError, match="KNOWLEDGE_OUTPUT_INTEGRITY_FAILED"):
            read_b_outputs(plans[-1][2]["plan.json"])


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda c: c[0].update(candidate_id="unknown"), "GENERATION_CANDIDATE_NOT_ELIGIBLE"),
        (lambda c: c[0].update(card_keys=["not-retrieved@1"]), "GENERATION_CARD_REFERENCE_INVALID"),
        (lambda c: c[0].update(path_ids=["invented"]), "GENERATION_PATH_REFERENCE_INVALID"),
        (lambda c: c.append(c[0]), "GENERATION_CANDIDATE_DUPLICATE"),
    ],
)
def test_invalid_model_reference_never_publishes_selected_plans(tmp_path, mutate, error):
    handoff, analysis, catalog = selection_inputs(tmp_path)
    output = tmp_path / "plans"
    output.mkdir()
    with pytest.raises(InputError, match=error):
        write_order_plans(
            handoff,
            analysis,
            catalog,
            repo=REPO,
            output=output,
            planning_config=config(),
            provider=ChoiceProvider(mutate=mutate),
        )
    assert not list(output.glob("*.plan.json"))


def test_budget_failure_precedes_model_and_zero_optional_slots_skip_call(tmp_path):
    handoff, analysis, catalog = selection_inputs(tmp_path)
    for pairs in (2, 3):
        output = tmp_path / f"plans-{pairs}"
        output.mkdir()
        provider = ChoiceProvider()
        settings = config(budget=SelectionBudget(max_test_pairs=pairs, max_c_runs=2 * pairs))
        if pairs == 2:
            with pytest.raises(InputError, match="MANDATORY_BUDGET_EXCEEDED"):
                write_order_plans(
                    handoff,
                    analysis,
                    catalog,
                    repo=REPO,
                    output=output,
                    planning_config=settings,
                    provider=provider,
                )
        else:
            plans = write_order_plans(
                handoff,
                analysis,
                catalog,
                repo=REPO,
                output=output,
                planning_config=settings,
                provider=provider,
            )
            assert len(plans) == 3
            assert plans[0][1].planner_metadata["selection"]["generation"]["called"] is False
        assert not provider.calls


def test_model_failure_keeps_failed_call_receipt(tmp_path):
    handoff, analysis, catalog = selection_inputs(tmp_path)
    output = tmp_path / "plans"
    output.mkdir()

    class BrokenProvider:
        def propose(self, context):
            raise InputError("GENERATION_RATE_LIMITED")

    with pytest.raises(InputError, match="GENERATION_RATE_LIMITED"):
        write_order_plans(
            handoff,
            analysis,
            catalog,
            repo=REPO,
            output=output,
            planning_config=config(),
            provider=BrokenProvider(),
        )
    receipt = json.loads((output / "generation-call.json").read_bytes())
    assert receipt["status"] == "FAILED" and receipt["called"] is True


def test_saved_proposal_replays_identical_selection_without_provider(tmp_path):
    handoff, analysis, catalog = selection_inputs(tmp_path)
    output = tmp_path / "plans"
    output.mkdir()
    plans = write_order_plans(
        handoff,
        analysis,
        catalog,
        repo=REPO,
        output=output,
        planning_config=config(),
        provider=ChoiceProvider(),
    )
    stored = read_b_outputs(plans[0][2]["plan.json"])
    original = plans[0][1].planner_metadata["selection"]
    replay = GenerationResult(Proposal.model_validate(original["proposal"]), original["generation"])
    result = select_trials(
        handoff,
        analysis,
        catalog,
        stored.retrieval,
        load_cards(REPO / ROOT / "cards"),
        registered_candidates(handoff, REPO),
        config(),
        provider=None,
        replay=replay,
    )
    assert result.model_dump(mode="json") == original


def test_actual_a_change_context_changes_selected_registered_trial(tmp_path):
    class ContextProvider(ChoiceProvider):
        def propose(self, context):
            values = json.dumps(context["evidence"]["change_values"])
            self.preferred = (
                "claimed-customer" if "json.body.customer_id" in values else "claimed-tenant"
            )
            return super().propose(context)

    family = json.loads(read_registered(REPO, f"{ROOT}/family.json"))
    actual_selected = []
    for pair in family["pairs"][2:4]:
        directory = tmp_path / pair["pair_id"]
        directory.mkdir()
        materialize_pair(REPO, pair, directory / "case")
        handoff = build_handoff(load_case(directory / "case"))
        analysis = analyze(handoff)
        catalog = TemplateCatalog.model_validate_json(read_registered(REPO, f"{ROOT}/catalog.json"))
        output = directory / "plans"
        output.mkdir()
        provider = ContextProvider()
        plans = write_order_plans(
            handoff,
            analysis,
            catalog,
            repo=REPO,
            output=output,
            planning_config=config(),
            provider=provider,
        )
        actual_selected.append(plans[-1][0])
        assert [p[0] for p in plans[:3]] == ["normal", "other-order", "claimed-owner"]
    assert actual_selected == ["claimed-tenant", "claimed-customer"]


@pytest.mark.parametrize("preferred", [None, "claimed-customer", "claimed-tenant"])
def test_vector_evidence_controls_optional_choice_and_empty_search_preserves_mandatory(
    tmp_path, monkeypatch, preferred
):
    from types import SimpleNamespace

    from flowdelta.ingestion import digest
    from flowdelta.knowledge import card_catalog, database, embedding_settings
    from flowdelta.knowledge.planning_config import FrozenCard

    from .test_vector_planner import PROFILE, _Provider

    handoff, analysis, catalog = selection_inputs(tmp_path)
    cards = load_cards(REPO / ROOT / "cards")
    settings = config().model_copy(
        update={
            "retrieval": "VECTOR",
            "embedding_profile_id": PROFILE.profile_id,
            "frozen_cards": [
                FrozenCard(
                    card_id=c.card_id,
                    version=c.version,
                    sha256=digest(json_bytes(c.model_dump(mode="json"))),
                )
                for c in cards
            ],
        }
    )
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setattr(
        database, "engine_for", lambda settings: SimpleNamespace(dispose=lambda: None)
    )
    monkeypatch.setattr(
        embedding_settings.EmbeddingSettings, "provider", lambda settings: _Provider()
    )

    def search(engine, vector, profile, limit, *, pinned_cards):
        return (
            [
                card_catalog.CardCandidate(
                    c,
                    pinned_cards[(c.card_id, c.version)],
                    0.9 if c.card_id == "order-" + preferred else 0.1,
                )
                for c in cards
            ]
            if preferred
            else []
        )

    monkeypatch.setattr(card_catalog, "search_cards", search)

    class EvidenceProvider:
        calls = 0

        def propose(self, context):
            self.calls += 1
            for hit in context["cards"]:
                for candidate in context["candidates"]:
                    if hit["card_key"] in candidate["card_keys"]:
                        return GenerationResult(
                            Proposal.model_validate(
                                {
                                    "choices": [
                                        {
                                            "candidate_id": candidate["candidate_id"],
                                            "card_keys": [hit["card_key"]],
                                            "path_ids": [],
                                            "reason": "Highest applicable retrieved evidence",
                                        }
                                    ]
                                }
                            ),
                            {"called": True, "provider": "DETERMINISTIC_TEST_DOUBLE"},
                        )
            pytest.fail("no applicable evidence")

    output = tmp_path / "plans"
    output.mkdir()
    provider = EvidenceProvider()
    plans = write_order_plans(
        handoff,
        analysis,
        catalog,
        repo=REPO,
        output=output,
        planning_config=settings,
        provider=provider,
    )
    assert [p[0] for p in plans[:3]] == ["normal", "other-order", "claimed-owner"]
    if preferred:
        assert plans[-1][0] == preferred and provider.calls == 1
    else:
        assert len(plans) == 3 and provider.calls == 0
        assert plans[0][1].planner_metadata["selection"]["search_status"] == "EMPTY_RETRIEVAL"
