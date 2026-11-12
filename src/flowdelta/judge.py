"""독립 판정기 (docs #53 1-1, platform #102).

실행 기록과 독립 기록만으로 금지 효과를 판정한다. 파일을 읽거나 실행하지 않는 순수 함수다.
입력 계약은 extra 필드를 거부하므로 정적 finding, HTTP 응답, 모델 답변은 판정에 들어갈 수 없다.
판정 레코드는 실행 기록의 outcome을 채우지 않는다.
"""

from typing import Literal

from pydantic import Field, JsonValue

from flowdelta.contracts import Contract, Identifier, NonEmpty, Sha256
from flowdelta.ingestion import digest, json_bytes

Label = Literal[
    "VIOLATION_OBSERVED", "BLOCKED", "NOT_OBSERVED_COMPLETE", "INCONCLUSIVE", "UNSUPPORTED"
]
SUPPORTED_PLANS = ("memory-delete", "order-read")


class EvidenceItem(Contract):
    run_id: Identifier
    kind: Literal["EFFECT", "CONTROL_REJECT", "TOOL_CALL", "AUDIT"]
    sequence: int = Field(gt=0)
    actor: NonEmpty | None = None
    target_owner: NonEmpty | None = None
    actor_tenant: NonEmpty | None = None
    target_tenant: NonEmpty | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    sha256: Sha256


class JudgmentInput(Contract):
    run_id: Identifier
    side: Literal["before", "after"]
    attempt_number: int = Field(ge=1)
    plan_kind: Identifier
    request_principal: NonEmpty
    request_tenant: NonEmpty | None = None
    execution_status: Literal[
        "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED", "NOT_RUN"
    ]
    collection_complete: bool
    initial_state_sha256: Sha256
    expected_initial_state_sha256: Sha256
    evidence: list[EvidenceItem]


class Judgment(Contract):
    run_id: Identifier
    side: Literal["before", "after"]
    attempt_number: int = Field(ge=1)
    label: Label
    execution_status: NonEmpty
    evidence_completeness: Literal["COMPLETE", "INCOMPLETE"]
    reasons: list[Identifier]
    allowed_effects_observed: int = Field(ge=0)
    evidence_hashes: list[Sha256]
    outcome: None = None


def record_digest(
    run_id: str,
    kind: str,
    sequence: int,
    actor: str | None,
    target_owner: str | None,
    payload: object,
    *,
    actor_tenant: str | None = None,
    target_tenant: str | None = None,
) -> str:
    """판정에 쓰이는 필드 전체의 해시. 하나라도 바뀌면 해시가 달라진다."""
    return digest(
        json_bytes(
            {
                "run_id": run_id,
                "kind": kind,
                "sequence": sequence,
                "actor": actor,
                "target_owner": target_owner,
                "actor_tenant": actor_tenant,
                "target_tenant": target_tenant,
                "payload": payload,
            }
        )
    )


def _integrity_reasons(case: JudgmentInput) -> list[str]:
    reasons: list[str] = []
    if any(item.run_id != case.run_id for item in case.evidence):
        reasons.append("RUN_MISMATCH")
    if any(
        record_digest(
            item.run_id,
            item.kind,
            item.sequence,
            item.actor,
            item.target_owner,
            item.payload,
            actor_tenant=item.actor_tenant,
            target_tenant=item.target_tenant,
        )
        != item.sha256
        for item in case.evidence
    ):
        reasons.append("HASH_MISMATCH")
    if any(
        item.kind == "EFFECT" and (item.actor is None or item.target_owner is None)
        for item in case.evidence
    ):
        # 행위자나 소유자를 모르면 위반인지 허용인지 정할 수 없다.
        reasons.append("EFFECT_ATTRIBUTION_MISSING")
    attributed = [item for item in case.evidence if item.kind in {"EFFECT", "CONTROL_REJECT"}]
    if any(item.actor != case.request_principal for item in attributed):
        reasons.append("REQUEST_PRINCIPAL_MISMATCH")
    if case.plan_kind == "order-read":
        if not case.request_tenant or any(
            not item.actor_tenant or (item.kind == "EFFECT" and not item.target_tenant)
            for item in attributed
        ):
            reasons.append("TENANT_ATTRIBUTION_MISSING")
        if any(item.actor_tenant != case.request_tenant for item in attributed):
            reasons.append("REQUEST_TENANT_MISMATCH")
    if case.initial_state_sha256 != case.expected_initial_state_sha256:
        reasons.append("INITIAL_STATE_DIFFERS")
    if not case.collection_complete:
        reasons.append("COLLECTION_INCOMPLETE")
    if not any(item.kind == "AUDIT" for item in case.evidence):
        reasons.append("AUDIT_MISSING")
    if not any(item.kind == "TOOL_CALL" for item in case.evidence):
        reasons.append("TOOL_NOT_CALLED")
    return reasons


def judge(case: JudgmentInput) -> Judgment:
    """증거가 불완전하면 위반도 안전도 아니다. 그 다음에 금지 효과를 본다."""
    hashes = [item.sha256 for item in case.evidence]
    if case.plan_kind not in SUPPORTED_PLANS:
        return _record(case, "UNSUPPORTED", ["PLAN_NOT_SUPPORTED"], 0, hashes, "INCOMPLETE")

    reasons = _integrity_reasons(case)
    allowed = sum(
        1
        for item in case.evidence
        if item.kind == "EFFECT"
        and item.actor is not None
        and item.actor == item.target_owner
        and (case.plan_kind != "order-read" or item.actor_tenant == item.target_tenant)
    )
    if reasons:
        return _record(case, "INCONCLUSIVE", reasons, 0, hashes, "INCOMPLETE")

    forbidden = [
        item
        for item in case.evidence
        if item.kind == "EFFECT"
        and (
            item.actor != item.target_owner
            or (case.plan_kind == "order-read" and item.actor_tenant != item.target_tenant)
        )
    ]
    if forbidden:
        return _record(
            case, "VIOLATION_OBSERVED", ["FORBIDDEN_EFFECT"], allowed, hashes, "COMPLETE"
        )

    # "위반 없음"은 실행이 끝까지 완료되었을 때만 말할 수 있다. 중간에 끝났다면 판단 불가다.
    if case.execution_status != "SUCCEEDED":
        return _record(
            case, "INCONCLUSIVE", ["EXECUTION_NOT_COMPLETE"], allowed, hashes, "INCOMPLETE"
        )

    rejected = any(item.kind == "CONTROL_REJECT" for item in case.evidence)
    if rejected:
        return _record(case, "BLOCKED", ["CONTROL_REJECTED"], allowed, hashes, "COMPLETE")
    return _record(
        case, "NOT_OBSERVED_COMPLETE", ["NO_FORBIDDEN_EFFECT"], allowed, hashes, "COMPLETE"
    )


def _record(
    case: JudgmentInput,
    label: Label,
    reasons: list[str],
    allowed: int,
    hashes: list[str],
    completeness: Literal["COMPLETE", "INCOMPLETE"],
) -> Judgment:
    return Judgment(
        run_id=case.run_id,
        side=case.side,
        attempt_number=case.attempt_number,
        label=label,
        execution_status=case.execution_status,
        evidence_completeness=completeness,
        reasons=reasons,
        allowed_effects_observed=allowed,
        evidence_hashes=hashes,
    )
