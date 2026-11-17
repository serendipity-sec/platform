"""Map independent service observations to a request-bound judgment.

A workflow request is not proof that a tool ran. Order ownership uses unchanged,
run-bound initial/final state. Unknown observation semantics remain inconclusive.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from flowdelta.ingestion import digest, json_bytes
from flowdelta.judge import EvidenceItem, JudgmentInput, record_digest


class AdapterError(ValueError):
    """Invalid evidence. Report an inconclusive result, never no violation."""


@dataclass(frozen=True)
class EffectRule:
    plan_kind: str
    effect_event_types: frozenset[str] = field(default_factory=frozenset)
    observation_event_types: frozenset[str] = field(default_factory=frozenset)
    effect_outcomes: frozenset[str] = frozenset({"RETURNED"})
    no_effect_outcomes: frozenset[str] = frozenset({"NOT_FOUND"})
    reject_outcomes: frozenset[str] = frozenset({"DENIED"})
    producer: str = "orders-api"
    order_id_field: str = "order_id"
    owner_field: str = "customer_id"
    tenant_field: str = "tenant_id"

    def __post_init__(self) -> None:
        groups = (self.effect_outcomes, self.no_effect_outcomes, self.reject_outcomes)
        if sum(map(len, groups)) != len(set().union(*groups)):
            raise ValueError("OUTCOME_RULE_OVERLAP")
        if self.effect_event_types & self.observation_event_types:
            raise ValueError("EVENT_RULE_OVERLAP")


def verified_content(artifact: Mapping[str, Any]) -> dict[str, Any]:
    content, expected = artifact.get("content"), artifact.get("sha256")
    if not isinstance(content, dict) or not isinstance(expected, str):
        raise AdapterError("ARTIFACT_MALFORMED")
    if digest(json_bytes(content)) != expected:
        raise AdapterError("ARTIFACT_HASH_MISMATCH")
    return content


def _state(artifact: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    doc = verified_content(artifact)
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("initial_sha256"), str):
        raise AdapterError("INITIAL_STATE_MISSING")
    if doc.get("run_id") != run_id or metadata.get("run_id") != run_id:
        raise AdapterError("STATE_RUN_MISMATCH")
    orders = doc.get("orders")
    if not isinstance(orders, list) or doc.get("orders_sha256") != digest(json_bytes(orders)):
        raise AdapterError("STATE_ORDERS_HASH_MISMATCH")
    return doc


def build_input(
    *,
    run_id: str,
    side: Literal["before", "after"],
    attempt_number: int,
    execution_status: Literal[
        "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED", "NOT_RUN"
    ],
    request_principal: str,
    request_tenant: str,
    events_artifact: Mapping[str, Any],
    initial_state_artifact: Mapping[str, Any],
    final_state_artifact: Mapping[str, Any],
    rule: EffectRule,
    expected_initial_sha256: str,
    collection_complete: bool = True,
    request_artifact: Mapping[str, Any] | None = None,
) -> JudgmentInput:
    events_doc = verified_content(events_artifact)
    initial_doc = _state(initial_state_artifact, run_id)
    final_doc = _state(final_state_artifact, run_id)
    if events_doc.get("run_id") != run_id:
        raise AdapterError("EVENT_DOCUMENT_RUN_MISMATCH")
    if request_artifact is not None:
        # Compatibility input only: it supplies neither identity nor TOOL_CALL evidence.
        request_doc = verified_content(request_artifact)
        if "run_id" in request_doc and request_doc["run_id"] != run_id:
            raise AdapterError("REQUEST_RUN_MISMATCH")
    raw_events = events_doc.get("events")
    if not isinstance(raw_events, list):
        raise AdapterError("EVENTS_MALFORMED")
    if any(not isinstance(event, dict) for event in raw_events):
        raise AdapterError("EVENT_MALFORMED")
    if any(type(event.get("sequence")) is not int for event in raw_events) or [
        event["sequence"] for event in raw_events
    ] != list(range(1, len(raw_events) + 1)):
        raise AdapterError("EVENT_SEQUENCE_INVALID")
    if final_doc.get("events") != raw_events:
        raise AdapterError("FINAL_EVENTS_MISMATCH")
    complete = (
        collection_complete
        and events_doc.get("collection_status") == "COMPLETE"
        and initial_doc.get("events") == []
        and initial_doc["metadata"]["initial_sha256"] == expected_initial_sha256
        and initial_doc["orders"] == final_doc["orders"]
    )
    owners: dict[str, dict[str, Any]] = {}
    for order in initial_doc["orders"]:
        if not isinstance(order, dict) or not isinstance(order.get(rule.order_id_field), str):
            raise AdapterError("ORDER_MALFORMED")
        identity = order[rule.order_id_field]
        if identity in owners:
            raise AdapterError("DUPLICATE_RESOURCE_ID")
        owners[identity] = order
    evidence: list[EvidenceItem] = []

    def append(
        kind: Literal["EFFECT", "CONTROL_REJECT", "TOOL_CALL", "AUDIT"],
        payload: dict[str, Any],
        actor: str | None = None,
        owner: str | None = None,
        actor_tenant: str | None = None,
        owner_tenant: str | None = None,
    ) -> None:
        sequence = len(evidence) + 1
        evidence.append(
            EvidenceItem(
                run_id=run_id,
                kind=kind,
                sequence=sequence,
                actor=actor,
                target_owner=owner,
                actor_tenant=actor_tenant,
                target_tenant=owner_tenant,
                payload=payload,
                sha256=record_digest(
                    run_id,
                    kind,
                    sequence,
                    actor,
                    owner,
                    payload,
                    actor_tenant=actor_tenant,
                    target_tenant=owner_tenant,
                ),
            )
        )

    for event in raw_events:
        if event.get("run_id") != run_id:
            raise AdapterError("EVENT_RUN_MISMATCH")
        event_type = event.get("event_type")
        if (
            not isinstance(event_type, str)
            or event.get("producer") != rule.producer
            or event_type not in rule.effect_event_types | rule.observation_event_types
        ):
            complete = False
            continue
        actor, tenant = event.get("principal"), event.get("tenant")
        if not isinstance(actor, str) or not actor or not isinstance(tenant, str) or not tenant:
            complete = False
            continue
        if (actor, tenant) != (request_principal, request_tenant):
            complete = False
        append("TOOL_CALL", event, actor=actor, actor_tenant=tenant)
        outcome = event.get("outcome")
        if not isinstance(outcome, str):
            complete = False
            continue
        if outcome in rule.no_effect_outcomes:
            continue  # NOT_FOUND is not proof of policy enforcement.
        if outcome not in rule.effect_outcomes | rule.reject_outcomes:
            complete = False
            continue
        if event_type in rule.observation_event_types:
            continue  # The selected policy/rule must explicitly permit metadata access.
        resource_id = event.get("resource_id")
        if not isinstance(resource_id, str) or not resource_id:
            raise AdapterError("EVENT_RESOURCE_MISSING")
        order = owners.get(resource_id, {})
        owner, owner_tenant = order.get(rule.owner_field), order.get(rule.tenant_field)
        owner = owner if isinstance(owner, str) and owner else None
        owner_tenant = owner_tenant if isinstance(owner_tenant, str) and owner_tenant else None
        attributes = event.get("attributes", {})
        if not isinstance(attributes, dict):
            raise AdapterError("EVENT_ATTRIBUTES_MALFORMED")
        for key, expected in (
            ("resource_customer_id", owner),
            ("resource_tenant_id", owner_tenant),
        ):
            if key in attributes and attributes[key] != expected:
                complete = False
        append(
            "EFFECT" if outcome in rule.effect_outcomes else "CONTROL_REJECT",
            event,
            actor,
            owner,
            tenant,
            owner_tenant,
        )
    append("AUDIT", events_doc)
    return JudgmentInput(
        run_id=run_id,
        side=side,
        attempt_number=attempt_number,
        plan_kind=rule.plan_kind,
        request_principal=request_principal,
        request_tenant=request_tenant,
        execution_status=execution_status,
        collection_complete=complete,
        initial_state_sha256=final_doc["metadata"]["initial_sha256"],
        expected_initial_state_sha256=expected_initial_sha256,
        evidence=evidence,
    )
