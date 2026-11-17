import json
from pathlib import Path

import pytest

from flowdelta.graph import analyze
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.knowledge.generation import GenerationResult, Proposal
from flowdelta.knowledge.order_plans import write_order_plans
from flowdelta.knowledge.planning_config import GenerationSettings, PlanningConfig, SelectionBudget
from flowdelta.knowledge.repository import export_b_outputs, read_b_outputs
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
