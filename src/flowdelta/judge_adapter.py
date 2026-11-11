"""실행 증거 묶음을 판정 입력(JudgmentInput)으로 바꾼다.

매핑 규칙 (docs #53 1-2 연결 계약의 초안):
- events.json: 독립 서비스 기록. AUDIT 항목으로 넣고, 규칙에 지정된 이벤트 종류만 EFFECT로 넣는다.
- EFFECT: principal(요청자)과 대상 주문의 소유자(최종 상태의 소유자 필드)를 비교한다.
- TOOL_CALL: 요청 기록(request.json)이 있으면 넣는다.
- 소유자를 찾지 못하거나 수집이 COMPLETE가 아니면 수집 미완료로 둔다.
  판정기가 INCONCLUSIVE로 판정한다.

아티팩트의 해시는 어댑터가 먼저 확인한다. 어긋나면 AdapterError를 던진다.
증거 불일치(다른 run, 해시 불일치 등)는 어댑터가 숨기지 않고 입력에 그대로 넘긴다.
판정기가 판단하게 한다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from flowdelta.ingestion import digest, json_bytes
from flowdelta.judge import EvidenceItem, JudgmentInput, record_digest


class AdapterError(ValueError):
    """증거를 읽을 수 없는 경우. 판정 입력을 만들지 않는다."""


@dataclass(frozen=True)
class EffectRule:
    """어떤 이벤트 종류를 효과로 볼지 정한다. 사례별 규칙은 호출자가 넘긴다."""

    plan_kind: str
    effect_event_types: frozenset[str] = field(default_factory=frozenset)
    order_id_field: str = "order_id"
    owner_field: str = "customer_id"


def verified_content(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """저장된 아티팩트의 내용을 꺼내고 해시를 확인한다."""
    content = artifact.get("content")
    expected = artifact.get("sha256")
    if not isinstance(content, dict) or not isinstance(expected, str):
        raise AdapterError("ARTIFACT_MALFORMED")
    if digest(json_bytes(content)) != expected:
        raise AdapterError("ARTIFACT_HASH_MISMATCH")
    return content


def _owner_of(final_doc: Mapping[str, Any], rule: EffectRule, resource_id: str) -> str | None:
    for order in final_doc.get("orders", []):
        if isinstance(order, dict) and order.get(rule.order_id_field) == resource_id:
            owner = order.get(rule.owner_field)
            return owner if isinstance(owner, str) else None
    return None


def build_input(
    *,
    run_id: str,
    side: Literal["before", "after"],
    attempt_number: int,
    execution_status: Literal[
        "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED", "NOT_RUN"
    ],
    events_artifact: Mapping[str, Any],
    final_state_artifact: Mapping[str, Any],
    request_artifact: Mapping[str, Any] | None,
    rule: EffectRule,
    expected_initial_sha256: str,
) -> JudgmentInput:
    events_doc = verified_content(events_artifact)
    final_doc = verified_content(final_state_artifact)

    complete = events_doc.get("collection_status") == "COMPLETE"
    raw_events = events_doc.get("events", [])
    evidence: list[EvidenceItem] = []
    sequence = 1

    for event in raw_events:
        if not isinstance(event, dict):
            raise AdapterError("EVENT_MALFORMED")
        event_run = event.get("run_id")
        event_seq = event.get("sequence")
        if not isinstance(event_run, str) or not isinstance(event_seq, int) or event_seq < 1:
            raise AdapterError("EVENT_MALFORMED")
        if event.get("event_type") not in rule.effect_event_types:
            continue
        principal = event.get("principal")
        owner = _owner_of(final_doc, rule, str(event.get("resource_id", "")))
        actor = principal if isinstance(principal, str) else None
        if owner is None:
            complete = False
        evidence.append(
            EvidenceItem(
                run_id=event_run,
                kind="EFFECT",
                sequence=event_seq,
                actor=actor,
                target_owner=owner,
                payload=event,
                sha256=record_digest(event_run, "EFFECT", event_seq, actor, owner, event),
            )
        )
        sequence = max(sequence, event_seq + 1)

    if request_artifact is not None:
        request_doc = verified_content(request_artifact)
        evidence.append(
            EvidenceItem(
                run_id=run_id,
                kind="TOOL_CALL",
                sequence=sequence,
                payload=request_doc,
                sha256=record_digest(run_id, "TOOL_CALL", sequence, None, None, request_doc),
            )
        )
        sequence += 1

    evidence.append(
        EvidenceItem(
            run_id=str(events_doc.get("run_id", "")),
            kind="AUDIT",
            sequence=sequence,
            payload=events_doc,
            sha256=record_digest(
                str(events_doc.get("run_id", "")), "AUDIT", sequence, None, None, events_doc
            ),
        )
    )

    metadata = final_doc.get("metadata", {})
    initial_sha = metadata.get("initial_sha256") if isinstance(metadata, dict) else None
    if not isinstance(initial_sha, str):
        raise AdapterError("INITIAL_STATE_MISSING")

    return JudgmentInput(
        run_id=run_id,
        side=side,
        attempt_number=attempt_number,
        plan_kind=rule.plan_kind,
        execution_status=execution_status,
        collection_complete=complete,
        initial_state_sha256=initial_sha,
        expected_initial_state_sha256=expected_initial_sha256,
        evidence=evidence,
    )
