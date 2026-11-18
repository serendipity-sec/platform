import copy
import json
from pathlib import Path

import pytest

from flowdelta.ingestion import digest, json_bytes
from flowdelta.judge import judge
from flowdelta.judge_adapter import AdapterError, EffectRule, build_input, verified_content

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "runtime/order-lookup/evidence/local-2026-10-11.json"
RULE = EffectRule(plan_kind="order-read", effect_event_types=frozenset({"ORDER_READ"}))


def artifact(content: dict) -> dict:
    return {"content": content, "sha256": digest(json_bytes(content)), "size_bytes": 0}


def real_run() -> dict:
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return data["comparisons"][0]["runs"][0]["embedded_artifacts"]


def synthetic(*, owner: str = "customer-demo", principal: str = "customer-demo") -> dict:
    run = "run-synth-1"
    event = {
        "event_type": "ORDER_READ",
        "producer": "orders-api",
        "tenant": "tenant-demo",
        "outcome": "RETURNED",
        "principal": principal,
        "resource_id": "ORD-1001",
        "run_id": run,
        "sequence": 1,
        "collection_status": "COMPLETE",
    }
    events = {"run_id": run, "collection_status": "COMPLETE", "events": [event]}
    final = {
        "run_id": run,
        "orders": [{"order_id": "ORD-1001", "customer_id": owner, "tenant_id": "tenant-demo"}],
        "events": [event],
        "metadata": {"initial_sha256": "e" * 64, "run_id": run},
    }
    final["orders_sha256"] = digest(json_bytes(final["orders"]))
    initial = copy.deepcopy(final)
    initial["events"] = []
    return {
        "run_id": run,
        "events": artifact(events),
        "final": artifact(final),
        "initial": artifact(initial),
        "request": artifact({"run_id": run, "method": "GET"}),
    }


def build(data: dict, **over):
    kwargs = {
        "run_id": data["run_id"],
        "side": "after",
        "attempt_number": 1,
        "execution_status": "SUCCEEDED",
        "request_principal": "customer-demo",
        "request_tenant": "tenant-demo",
        "events_artifact": data["events"],
        "final_state_artifact": data["final"],
        "initial_state_artifact": data["initial"],
        "request_artifact": data.get("request"),
        "rule": RULE,
        "expected_initial_sha256": "e" * 64,
    }
    kwargs.update(over)
    return build_input(**kwargs)


def test_real_committed_run_is_judged_without_a_forbidden_effect() -> None:
    arts = real_run()
    run_id = verified_content(arts["events.json"])["run_id"]
    expected = verified_content(arts["final-state.json"])["metadata"]["initial_sha256"]
    case = build_input(
        run_id=run_id,
        side="after",
        attempt_number=1,
        execution_status="SUCCEEDED",
        request_principal="customer-demo",
        request_tenant="tenant-demo",
        events_artifact=arts["events.json"],
        final_state_artifact=arts["final-state.json"],
        initial_state_artifact=arts["initial-state.json"],
        request_artifact=arts["request.json"],
        rule=RULE,
        expected_initial_sha256=expected,
    )
    result = judge(case)
    assert result.label == "NOT_OBSERVED_COMPLETE"
    assert result.allowed_effects_observed == 1  # 고객이 자기 주문을 조회한 허용된 효과 1건


def test_another_customers_order_read_becomes_a_forbidden_effect() -> None:
    data = synthetic(owner="customer-other", principal="customer-demo")
    result = judge(build(data))
    assert result.label == "VIOLATION_OBSERVED"


def test_own_order_read_is_allowed_and_counted() -> None:
    result = judge(build(synthetic()))
    assert result.label == "NOT_OBSERVED_COMPLETE"
    assert result.allowed_effects_observed == 1


def test_order_without_known_owner_is_inconclusive() -> None:
    data = synthetic()
    final = verified_content(data["final"])
    final["orders"] = []
    final["orders_sha256"] = digest(json_bytes(final["orders"]))
    data["final"] = artifact(final)
    result = judge(build(data))
    assert result.label == "INCONCLUSIVE"
    assert "COLLECTION_INCOMPLETE" in result.reasons


def test_tampered_artifact_is_refused_before_any_judgment() -> None:
    data = synthetic()
    data["events"]["content"]["events"][0]["principal"] = "customer-other"
    with pytest.raises(AdapterError, match="ARTIFACT_HASH_MISMATCH"):
        build(data)


def test_incomplete_collection_is_inconclusive() -> None:
    data = synthetic()
    events = copy.deepcopy(verified_content(data["events"]))
    events["collection_status"] = "PARTIAL"
    data["events"] = artifact(events)
    result = judge(build(data))
    assert result.label == "INCONCLUSIVE"
    assert "COLLECTION_INCOMPLETE" in result.reasons


def test_run_mismatch_is_passed_on_for_the_judge_to_refuse() -> None:
    data = synthetic()
    events = copy.deepcopy(verified_content(data["events"]))
    events["run_id"] = "run-other"
    data["events"] = artifact(events)
    with pytest.raises(AdapterError, match="EVENT_DOCUMENT_RUN_MISMATCH"):
        build(data)


def test_missing_initial_state_is_refused() -> None:
    data = synthetic()
    final = verified_content(data["final"])
    final["metadata"] = {"run_id": data["run_id"]}
    data["final"] = artifact(final)
    with pytest.raises(AdapterError, match="INITIAL_STATE_MISSING"):
        build(data)


def replace_events(data, events):
    doc = copy.deepcopy(data["events"]["content"])
    doc["events"] = events
    data["events"] = artifact(doc)
    final = copy.deepcopy(data["final"]["content"])
    final["events"] = events
    data["final"] = artifact(final)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("RETURNED", "VIOLATION_OBSERVED"),
        ("NOT_FOUND", "NOT_OBSERVED_COMPLETE"),
        ("DENIED", "BLOCKED"),
        ("UNKNOWN", "INCONCLUSIVE"),
        (None, "INCONCLUSIVE"),
    ],
)
def test_service_outcome_controls_effect_mapping(outcome, expected):
    data = synthetic(owner="other-demo")
    event = {**data["events"]["content"]["events"][0], "outcome": outcome}
    replace_events(data, [event])
    assert judge(build(data)).label == expected


def test_request_without_service_event_does_not_prove_tool_call():
    data = synthetic()
    replace_events(data, [])
    result = judge(build(data))
    assert result.label == "INCONCLUSIVE"
    assert "TOOL_NOT_CALLED" in result.reasons


@pytest.mark.parametrize(
    "which,location",
    [
        ("initial", "root"),
        ("initial", "metadata"),
        ("final", "root"),
        ("final", "metadata"),
    ],
)
def test_state_from_another_run_is_rejected_even_with_valid_hash(which, location):
    data = synthetic()
    doc = copy.deepcopy(data[which]["content"])
    (doc if location == "root" else doc["metadata"])["run_id"] = "run-unrelated"
    data[which] = artifact(doc)
    with pytest.raises(AdapterError, match="STATE_RUN_MISMATCH"):
        build(data)


def test_owner_change_cannot_turn_prior_foreign_read_into_allowed_effect():
    data = synthetic(owner="other-demo")
    final = copy.deepcopy(data["final"]["content"])
    final["orders"][0]["customer_id"] = "customer-demo"
    final["orders_sha256"] = digest(json_bytes(final["orders"]))
    data["final"] = artifact(final)
    result = judge(build(data))
    assert result.label == "INCONCLUSIVE" and result.allowed_effects_observed == 0


def test_same_customer_id_in_different_tenant_is_a_forbidden_read():
    data = synthetic()
    for which in ("initial", "final"):
        doc = copy.deepcopy(data[which]["content"])
        doc["orders"][0]["tenant_id"] = "other-tenant"
        doc["orders_sha256"] = digest(json_bytes(doc["orders"]))
        data[which] = artifact(doc)
    assert judge(build(data)).label == "VIOLATION_OBSERVED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal", "other-demo"),
        ("tenant", "other-tenant"),
        ("producer", "workflow"),
        ("outcome", {}),
        ("event_type", []),
    ],
)
def test_unknown_or_wrong_source_context_cannot_become_no_violation(field, value):
    data = synthetic()
    replace_events(data, [{**data["events"]["content"]["events"][0], field: value}])
    assert judge(build(data)).label == "INCONCLUSIVE"


@pytest.mark.parametrize("sequence", [0, 2, True, "1"])
def test_missing_or_invalid_event_sequence_is_rejected(sequence):
    data = synthetic()
    replace_events(data, [{**data["events"]["content"]["events"][0], "sequence": sequence}])
    with pytest.raises(AdapterError, match="EVENT_SEQUENCE_INVALID"):
        build(data)


def test_final_event_document_disagreement_is_rejected():
    data = synthetic()
    final = copy.deepcopy(data["final"]["content"])
    final["events"] = []
    data["final"] = artifact(final)
    with pytest.raises(AdapterError, match="FINAL_EVENTS_MISMATCH"):
        build(data)


def test_metadata_is_not_a_content_read_and_needs_an_explicit_policy_rule():
    data = synthetic(owner="other-demo")
    replace_events(
        data, [{**data["events"]["content"]["events"][0], "event_type": "ORDER_METADATA_READ"}]
    )
    assert judge(build(data)).label == "INCONCLUSIVE"
    metadata_rule = EffectRule(
        plan_kind="order-read",
        effect_event_types=frozenset({"ORDER_READ"}),
        observation_event_types=frozenset({"ORDER_METADATA_READ"}),
    )
    result = judge(build(data, rule=metadata_rule))
    assert result.label == "NOT_OBSERVED_COMPLETE" and result.allowed_effects_observed == 0


def test_failed_execution_after_not_found_is_inconclusive_not_blocked():
    data = synthetic(owner="other-demo")
    replace_events(data, [{**data["events"]["content"]["events"][0], "outcome": "NOT_FOUND"}])
    result = judge(build(data, execution_status="FAILED"))
    assert result.label == "INCONCLUSIVE" and "EXECUTION_NOT_COMPLETE" in result.reasons


def test_runtime_collection_status_is_required_even_when_events_claim_complete():
    assert judge(build(synthetic(), collection_complete=False)).label == "INCONCLUSIVE"


def test_conflicting_mapping_rules_are_rejected():
    with pytest.raises(ValueError, match="OUTCOME_RULE_OVERLAP"):
        EffectRule(plan_kind="order-read", reject_outcomes=frozenset({"RETURNED"}))
