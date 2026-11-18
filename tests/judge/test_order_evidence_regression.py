"""Regress #109/#117 against byte-pinned n8n observations from #113."""

import json
from pathlib import Path

import pytest

from flowdelta.ingestion import digest
from flowdelta.judge import Label, judge
from flowdelta.judge_adapter import EffectRule, build_input

FIXTURES = Path(__file__).with_name("fixtures") / "ownership-family-4b4dba5"
RULE = EffectRule(plan_kind="order-read", effect_event_types=frozenset({"ORDER_READ"}))


@pytest.mark.parametrize(
    ("filename", "source_sha256", "service_outcome", "expected_label"),
    [
        (
            "v0-service-check__other-order.json",
            "1eed48feb549d26996792a2491b744704d55edbedc38d13d27914c33fcd6c857",
            "NOT_FOUND",
            "NOT_OBSERVED_COMPLETE",
        ),
        (
            "v1-internal-no-owner__other-order.json",
            "a77c6474a1056d4c32f433103fe02ad35f8b589e6202de58cfca3031ef3e5313",
            "RETURNED",
            "VIOLATION_OBSERVED",
        ),
    ],
    ids=["not-found-is-neither-read-nor-policy-block", "foreign-read-is-a-violation"],
)
def test_recorded_foreign_order_evidence(
    filename: str, source_sha256: str, service_outcome: str, expected_label: Label
) -> None:
    raw = (FIXTURES / filename).read_bytes()
    assert digest(raw) == source_sha256
    recorded = json.loads(raw)
    artifacts = recorded["embedded_artifacts"]
    case = build_input(
        run_id=recorded["run_id"],
        side="after",
        attempt_number=1,
        execution_status=recorded["execution_status"],
        # The fixed experiment authenticates this identity, never the request body/event actor.
        request_principal="customer-demo",
        request_tenant="tenant-demo",
        initial_state_artifact=artifacts["initial-state.json"],
        final_state_artifact=artifacts["final-state.json"],
        events_artifact=artifacts["events.json"],
        request_artifact=artifacts["request.json"],
        expected_initial_sha256=recorded["initial_state_file_sha256"],
        rule=RULE,
    )
    calls = [item for item in case.evidence if item.kind == "TOOL_CALL"]
    assert len(calls) == 1
    assert calls[0].payload["outcome"] == service_outcome
    assert calls[0].payload["resource_id"] == "ORD-2001"
    assert not any(item.kind == "CONTROL_REJECT" for item in case.evidence)
    effects = [item for item in case.evidence if item.kind == "EFFECT"]
    if service_outcome == "NOT_FOUND":
        assert effects == []
    else:
        assert len(effects) == 1
        assert (effects[0].actor, effects[0].actor_tenant) == ("customer-demo", "tenant-demo")
        assert (effects[0].target_owner, effects[0].target_tenant) == ("other-demo", "other-tenant")

    result = judge(case)
    assert result.label == expected_label
    assert result.evidence_completeness == "COMPLETE"
    assert result.allowed_effects_observed == 0
    assert (result.run_id, result.side, result.attempt_number) == (recorded["run_id"], "after", 1)
    assert result.outcome is None
