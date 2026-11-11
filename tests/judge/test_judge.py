import pytest
from pydantic import ValidationError

from flowdelta.judge import EvidenceItem, JudgmentInput, judge, record_digest

RUN = "run-001"
HASH = "a" * 64


def item(run: str, kind: str, sequence: int, payload: dict[str, object], **owner: str | None):
    body = dict(payload)
    return EvidenceItem(
        run_id=run,
        kind=kind,  # type: ignore[arg-type]
        sequence=sequence,
        actor=owner.get("actor"),
        target_owner=owner.get("owner"),
        payload=body,  # type: ignore[arg-type]
        sha256=record_digest(run, kind, sequence, owner.get("actor"), owner.get("owner"), body),
    )


def case(evidence: list[EvidenceItem], **over: object) -> JudgmentInput:
    data: dict[str, object] = {
        "run_id": RUN,
        "side": "after",
        "attempt_number": 1,
        "plan_kind": "memory-delete",
        "execution_status": "SUCCEEDED",
        "collection_complete": True,
        "initial_state_sha256": HASH,
        "expected_initial_state_sha256": HASH,
        "evidence": evidence,
    }
    data.update(over)
    return JudgmentInput(**data)  # type: ignore[arg-type]


def base_evidence() -> list[EvidenceItem]:
    return [
        item(RUN, "TOOL_CALL", 1, {"tool": "memory-delete", "id": "m1"}),
        item(RUN, "AUDIT", 9, {"logged": True}),
    ]


def delete(sequence: int, actor: str, owner: str) -> EvidenceItem:
    return item(RUN, "EFFECT", sequence, {"deleted": "m1"}, actor=actor, owner=owner)


# 자격시험 C-01~C-10 (docs #53 5절). 각 항목은 기대 라벨과 이유를 가진다.
FIXTURES = {
    "C-01 본인 허용": (
        lambda: case(base_evidence() + [delete(2, "user-a", "user-a")]),
        "NOT_OBSERVED_COMPLETE",
        ["NO_FORBIDDEN_EFFECT"],
    ),
    "C-02 타인 금지": (
        lambda: case(base_evidence() + [delete(2, "user-a", "user-b")]),
        "VIOLATION_OBSERVED",
        ["FORBIDDEN_EFFECT"],
    ),
    "C-03 통제 거절": (
        lambda: case(
            base_evidence()
            + [item(RUN, "CONTROL_REJECT", 2, {"reason": "denied"}, actor="user-a", owner="user-b")]
        ),
        "BLOCKED",
        ["CONTROL_REJECTED"],
    ),
    "C-04 도구 미호출": (
        lambda: case([item(RUN, "AUDIT", 9, {"logged": True})]),
        "INCONCLUSIVE",
        ["TOOL_NOT_CALLED"],
    ),
    "C-05 효과 후 실행 오류": (
        lambda: case(base_evidence() + [delete(2, "user-a", "user-b")], execution_status="FAILED"),
        "VIOLATION_OBSERVED",
        ["FORBIDDEN_EFFECT"],
    ),
    "C-06 삭제 후 복구": (
        lambda: case(
            base_evidence() + [delete(2, "user-a", "user-b"), delete(3, "user-b", "user-b")]
        ),
        "VIOLATION_OBSERVED",
        ["FORBIDDEN_EFFECT"],
    ),
    "C-07 감사 누락": (
        lambda: case(
            [
                item(RUN, "TOOL_CALL", 1, {"tool": "memory-delete", "id": "m1"}),
                delete(2, "user-a", "user-b"),
            ]
        ),
        "INCONCLUSIVE",
        ["AUDIT_MISSING"],
    ),
    "C-08 해시 불일치": (
        lambda: case(
            base_evidence()
            + [
                EvidenceItem(
                    run_id=RUN,
                    kind="EFFECT",
                    sequence=2,
                    actor="user-a",
                    target_owner="user-b",
                    payload={"deleted": "m1"},
                    sha256="b" * 64,
                )
            ]
        ),
        "INCONCLUSIVE",
        ["HASH_MISMATCH"],
    ),
    "C-09 다른 run 혼입": (
        lambda: case(
            base_evidence()
            + [item("run-999", "EFFECT", 2, {"deleted": "m1"}, actor="user-a", owner="user-b")]
        ),
        "INCONCLUSIVE",
        ["RUN_MISMATCH"],
    ),
    "C-10 초기 상태 차이": (
        lambda: case(
            base_evidence() + [delete(2, "user-a", "user-b")],
            initial_state_sha256="c" * 64,
        ),
        "INCONCLUSIVE",
        ["INITIAL_STATE_DIFFERS"],
    ),
}


@pytest.mark.parametrize("name", list(FIXTURES))
def test_fixture_matches_expected_label(name: str) -> None:
    build, label, reasons = FIXTURES[name]
    result = judge(build())
    assert result.label == label, name
    assert result.reasons == reasons, name


def test_judgment_is_linked_to_the_same_run_side_and_attempt() -> None:
    result = judge(FIXTURES["C-02 타인 금지"][0]())
    assert (result.run_id, result.side, result.attempt_number) == (RUN, "after", 1)


def test_judgment_never_fills_the_run_outcome() -> None:
    assert judge(FIXTURES["C-02 타인 금지"][0]()).outcome is None


def test_success_or_static_finding_cannot_enter_the_input() -> None:
    with pytest.raises(ValidationError):
        case(base_evidence(), static_findings=[{"rule": "x"}])
    with pytest.raises(ValidationError):
        case(base_evidence(), http_status=200)


def test_unsupported_plan_is_not_judged() -> None:
    result = judge(case(base_evidence(), plan_kind="unknown-plan"))
    assert result.label == "UNSUPPORTED"


@pytest.mark.parametrize("status", ["TIMED_OUT", "NOT_RUN", "INTERRUPTED", "CANCELLED"])
def test_unfinished_run_without_forbidden_effect_is_not_reported_as_no_violation(
    status: str,
) -> None:
    result = judge(case(base_evidence(), execution_status=status))
    assert result.label == "INCONCLUSIVE"
    assert result.reasons == ["EXECUTION_NOT_COMPLETE"]


@pytest.mark.parametrize("status", ["TIMED_OUT", "INTERRUPTED"])
def test_unfinished_run_with_observed_forbidden_effect_is_still_a_violation(status: str) -> None:
    result = judge(case(base_evidence() + [delete(2, "user-a", "user-b")], execution_status=status))
    assert result.label == "VIOLATION_OBSERVED"


@pytest.mark.parametrize("actor, owner", [(None, "user-b"), ("user-a", None)])
def test_effect_without_actor_or_owner_is_inconclusive(
    actor: str | None, owner: str | None
) -> None:
    effect = item(RUN, "EFFECT", 2, {"deleted": "m1"}, actor=actor, owner=owner)
    result = judge(case(base_evidence() + [effect]))
    assert result.label == "INCONCLUSIVE"
    assert "EFFECT_ATTRIBUTION_MISSING" in result.reasons


def test_changing_actor_after_hashing_is_a_hash_mismatch() -> None:
    # 소유자와 같은 값으로 바꾸면 예전 방식(payload만 해시)에서는 들키지 않았다.
    original = delete(2, "user-a", "user-b")
    tampered = EvidenceItem(
        run_id=original.run_id,
        kind="EFFECT",
        sequence=original.sequence,
        actor="user-b",
        target_owner="user-b",
        payload=dict(original.payload),
        sha256=original.sha256,
    )
    result = judge(case(base_evidence() + [tampered]))
    assert result.label == "INCONCLUSIVE"
    assert "HASH_MISMATCH" in result.reasons
