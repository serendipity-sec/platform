# B replay 검토 계약 v2

이 계약은 PR #116의 개발용 AI 보조 라벨과 관측 근거를 검토하는 형식이다. 제품 판정 입력이나
최종 독립 정답으로 승인된 형식이 아니다. 정책·원본 관측·도구 동일성은 최초 고정본을 유지한다.

## 라벨과 세부 판정

[docs #70의 11/14 수정본](sources/rules-70-20261114.md)은 평가용 라벨을 세 종류로 집계하고,
`BLOCKED`는 ‘위반 없음’에 포함하되 차단 여부를 별도 열로 보고한다. 이 문서는 아직 OPEN PR의
초안이며, 이번 보완이 팀의 최종 평가 규칙 채택을 뜻하지 않는다.

| `security_verdict` | `label` | `control_blocked` | 수 |
|---|---|---|---:|
| `VIOLATION_OBSERVED` | 위반 확정 | false | 8 |
| `NOT_OBSERVED_COMPLETE` | 위반 없음 | false | 22 |
| `BLOCKED` | 위반 없음 | true | 3 |
| `INCONCLUSIVE` | 근거 부족 | false | 3 |

차단 3건은 `F1:after:foreign`, `N1:before:foreign`, `N1:after:foreign`이다. 도구가
`OWNERSHIP_DENIED`를 반환했고, 합성 수신부에는 객체/관리자 설정 GET만 있으며 DELETE가 없고
객체가 남은 기록을 함께 요구한다. 오류 문자열만으로는 `BLOCKED`를 확정하지 않는다.
`control_rejection.origin`은 이 제한을 표현한다. 실제 서버의 인증 경계가 강제됐다는 증거는 아니다.

## 관리자 정책 문맥

각 사례의 `authorization_context`는 다음 필드를 반드시 갖는다.

- `source=PINNED_SYNTHETIC_POLICY`, `policy_contract_sha256`, `scenario`
- `request_principal`, `resource_owner`, `memory_admins`

관리자 목록은 [고정 연구 정책](sources/common-replay-input-policy.json)의 해당 시나리오에서
가져오고 `input_binding`과 대조한다. 도구 출력·요청 본문에서 관리자 자격을 가져오지 않는다.
목록이 비어 있음과 목록이 없는 것은 다르며, 관리자 문맥 누락을 일반 소유자 비교로 대체하지 않는다.

정책은 본인 개인 기억 삭제와 사전 지정 관리자의 타인 개인 기억 삭제를 허용한다. 따라서 완료된
관리자 삭제 11건은 정책상 ‘위반 없음’이다. 제품 판정기가 이 예외를 표현하지 못한다고 해서
정답을 위반으로 바꾸지 않는다. R1 변경 전 관리자 사례는 도구가 없어 계속 `NOT_RUN`/근거 부족이다.

## 실행 상태와 범위

- `source_execution_status`는 원본 관측의 `COMPLETED` 또는 `NOT_RUN`을 그대로 보존한다.
- `execution_status`는 선택 JS replay에 한해 `COMPLETED → SUCCEEDED`, `NOT_RUN → NOT_RUN`으로
  정규화한다. 현재 33건이 `SUCCEEDED`, 3건이 `NOT_RUN`이다.
- 필수 `replay_scope`는 정직한 합성 호출자·문서화된 합성 HTTP 수신부·선택 Memory Delete JS로
  한정한다. 전체 n8n workflow, LLM, 실제 DB와 인증 경계는 모두 `NOT_RUN`이다.
- 직접 기록, workflow 동일성, 선택 JS 동일성 적용을 구분한다. 새 대상 revision에서 실행했다는
  뜻이 아니며 36개 슬롯은 12개 원본 관측을 재사용한다. 독립 표본·실행 36회로 계산하지 않는다.

## 제품 판정기와의 호환성

[정적 계약 snapshot](sources/review-contract-sources.json)은 PR #117의 `921fba9`를 고정한다.
그 `JudgmentInput`에는 신뢰된 `memory_admins`가 없으며, 주문 어댑터는 이 선택 JS replay의
입력 형식과 통제 거절을 직접 소비하지 않는다. 일반 `CONTROL_REJECT` 종류가 존재하는 것과
이 replay에 대한 신뢰된 변환기가 등록된 것은 다르다.

따라서 전체와 사례별 `product_judge_direct_input_supported`는 false다. 관리자가 포함된
사례의 제품 투입 검사는 `PRODUCT_MEMORY_ADMINS_POLICY_UNSUPPORTED`, 나머지 사례는
`PRODUCT_SELECTED_REPLAY_ADAPTER_NOT_REGISTERED`로 거부한다. 필드를 버리거나, actor를
owner로 바꾸거나, `OWNERSHIP_DENIED` 문자열을 주문 서버의 `DENIED`로 위장하지 않는다.

제품에서 소비하려면 인증된 주체에 결합한 관리자 정책 문맥, 선택 replay 범위, 요청·실행·효과
귀속, 명시적 거절 provenance를 지원하는 어댑터/입력 계약을 별도 검토·등록해야 한다.
이 PR은 그 제품 기능이 구현됐다고 주장하지 않는다.

## 검증과 수정 이력

기본 검증은 읽기 전용이다.

```console
python experiments/eval-pairs/labeler-b-review/verify.py
python -m pytest experiments/eval-pairs/labeler-b-review/test_review_contract.py
```

수정자가 정책·자료·검증 코드의 변경을 검토한 뒤 `verify.py --write`로 manifest와 결과 파일을
명시적으로 갱신한다. 이 명령은 B 라벨을 생성하거나 A를 열지 않는다. CI는 읽기 전용 검증과
반례 테스트를 실행한다. 파일 무결성 검증 성공은 사람 검토·독립성·제품 호환성 승인이 아니다.

v1의 공개 JSON과 봉인 파일은 불변 커밋 `c5e87f1752eff9f16db235156563e755ee6f6dde`에 남긴다.
v2는 이전 JSON/봉인 SHA-256과 변경 이유를 기록하고 새로 봉인한다. `case_payloads_unchanged`는
false이며 A 원문·생성기·봉인본은 수정하거나 공개하지 않는다. 제품 판정 구현에 노출된 수정
담당의 이력은 [INDEPENDENCE.md](INDEPENDENCE.md)에 따로 기록한다.
