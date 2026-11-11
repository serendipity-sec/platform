import copy
import json
from pathlib import Path

import pytest

from flowdelta.ingestion import digest, json_bytes
from flowdelta.judge import judge
from flowdelta.judge_adapter import AdapterError, EffectRule, build_input, verified_content

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "runtime/order-lookup/evidence/local-2026-10-11.json"
RULE = EffectRule(plan_kind="memory-delete", effect_event_types=frozenset({"ORDER_READ"}))


def artifact(content: dict) -> dict:
    return {"content": content, "sha256": digest(json_bytes(content)), "size_bytes": 0}


def real_run() -> dict:
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return data["comparisons"][0]["runs"][0]["embedded_artifacts"]


def synthetic(*, owner: str = "customer-demo", principal: str = "customer-demo") -> dict:
    run = "run-synth-1"
    event = {
        "event_type": "ORDER_READ",
        "principal": principal,
        "resource_id": "ORD-1001",
        "run_id": run,
        "sequence": 1,
        "collection_status": "COMPLETE",
    }
    events = {"run_id": run, "collection_status": "COMPLETE", "events": [event]}
    final = {
        "orders": [{"order_id": "ORD-1001", "customer_id": owner}],
        "metadata": {"initial_sha256": "e" * 64, "run_id": run},
    }
    return {
        "run_id": run,
        "events": artifact(events),
        "final": artifact(final),
        "request": artifact({"run_id": run, "method": "GET"}),
    }


def build(data: dict, **over):
    kwargs = {
        "run_id": data["run_id"],
        "side": "after",
        "attempt_number": 1,
        "execution_status": "SUCCEEDED",
        "events_artifact": data["events"],
        "final_state_artifact": data["final"],
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
        events_artifact=arts["events.json"],
        final_state_artifact=arts["final-state.json"],
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
    result = judge(build(data))
    assert result.label == "INCONCLUSIVE"
    assert "RUN_MISMATCH" in result.reasons


def test_missing_initial_state_is_refused() -> None:
    data = synthetic()
    final = verified_content(data["final"])
    final["metadata"] = {"run_id": data["run_id"]}
    data["final"] = artifact(final)
    with pytest.raises(AdapterError, match="INITIAL_STATE_MISSING"):
        build(data)
