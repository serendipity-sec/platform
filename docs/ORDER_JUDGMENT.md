# 등록 주문 조회의 독립 판정 연결

관련: [#115](https://github.com/serendipity-sec/platform/issues/115),
[#103](https://github.com/serendipity-sec/platform/pull/103),
[#109](https://github.com/serendipity-sec/platform/pull/109).

기존 A→B→C 주문 파이프라인에 **선택적인 판정 단계**를 연결한다. 정상 업무 검증과 실행
상태는 기존대로 유지하며, `ExecutionRun.outcome`과 summary의 `security_outcome`은 null이다.
실제 읽기·증거 완전성에 대한 판정은 별도 `judgments/report.json`에 저장한다.

## 실행

저장소 루트, Python 3.13, Docker에서:

```sh
uv sync --frozen --extra baseline --extra knowledge
docker build --tag flowdelta-order-fixture:0.3.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline --extra knowledge flowdelta demo-order-lookup --judge-order --out var/order-judgment
uv run --frozen --extra baseline --extra knowledge flowdelta check-pipeline var/order-judgment/bundle
```

n8n은 기존 runtime lock에 고정된 이미지를 준비한다. 일반 `run-pipeline` 명령에도
`--execute --judge-order`를 함께 붙일 수 있다. `--judge-order`만으로 실행을 요청하지 않은
계획을 판정하지 않는다. 선택 옵션을 생략하면 기존 실행 방식과 출력 계약을 유지한다.

이미 실행한 bundle은 workflow를 재실행하지 않고 확인한다:

```sh
uv run --frozen --extra baseline flowdelta judge-order-bundle var/order-judgment/bundle --out var/order-recheck
```

마지막 명령은 `var/order-recheck/report.json`에 저장한다. 새 출력 경로만 허용하며 기존 bundle,
request/response, 실행 레코드를 수정하지 않는다. 준비만 된 bundle은 `NOT_RUN`이며 판단한 실행이 없다.
불완전한 판정은 종료 코드 2다. 무결성이 깨져 실행 식별자를 믿을 수 없는 bundle은 입력 오류로 거부한다.

## 근거와 판정 경계

현재 연결은 **등록된 합성 정상 주문 조회의 own-order 읽기 규칙**만 지원한다. 임의 사례나
변형 workflow를 이 규칙으로 자동 평가하지 않는다. C preflight의 workflow/state allowlist와
지원 의미 검사를 재사용하고, task·policy·environment 등을 포함한 source manifest 전체를
`runtime/order-lookup/approved-inputs.json`의 등록 해시와 대조한다.

요청 주체 `customer-demo`와 테넌트 `tenant-demo`는 이 등록 profile의 fixture 서비스 binding이다.
**등록 fixture 전용이며, 다른 호출자나 테넌트에 그대로 재사용하지 않는다.** 사용자 입력으로
이 상수를 바꾸거나, 실제 사용자의 인증 정보 대신 이 값을 전달해서도 안 된다.
요청 본문, 최종 HTTP 응답, 이벤트에 적힌 임의 사용자 값을 요청 주체로 채택하지 않는다.
다른 profile은 인증 출처와 규칙을 별도로 등록해야 한다.

1. bundle과 실행 레코드의 참조·파일 해시, run/side/attempt/plan 연결을 확인한다.
2. 초기/최종 상태의 top-level 및 metadata run_id, 실제 orders digest, 고정 초기 파일의 digest를
   확인한다. 초기 orders 내용도 등록된 입력과 비교한다. 조회 중 소유자가 바뀌면 최종 소유자로
   과거 효과를 소급 판정하지 않고 `INCONCLUSIVE`로 남긴다.
3. events 문서와 최종 상태의 이벤트 목록이 일치하고 sequence가 연속인지 확인한다.
   다른 run, 누락·중복, 알 수 없는 producer/event/outcome을 정상 관측으로 넘기지 않는다.
4. 실제 서비스 이벤트에서만 `TOOL_CALL`을 만든다. 입력 `request.json`은 도구 호출 증거가 아니다.
5. `ORDER_READ`의 `RETURNED`만 내용 읽기 효과다. `NOT_FOUND`는 반환 없는 조회이고,
   그것만으로 정책에 의해 차단됐다고 확정하지 않는다. 명시적인 `DENIED`는 `CONTROL_REJECT`다.
6. EFFECT의 principal·tenant가 신뢰된 요청 주체와 맞아야 한다. 실제 소유자의 customer와
   tenant 중 하나라도 다르면 금지 읽기다. 소유자/귀속이 없거나 증거가 불완전하면 판단 불가다.

완전한 증거의 금지 효과는 실행 오류 뒤에도 `VIOLATION_OBSERVED`다. 효과가 없을 때
`BLOCKED`/`NOT_OBSERVED_COMPLETE`는 실행 `SUCCEEDED`를 요구한다. 불완전한 근거의 허용
효과 개수는 0으로 남기며 허용이 확인됐다고 표시하지 않는다.

`NOT_FOUND`만으로는 주문 부재와 권한에 의한 은폐를 구별할 수 없으므로 `BLOCKED`로
바꾸지 않는다. 현재 규칙은 다음과 같다.

| 독립 서비스 기록과 실행 상태 | 판정 | 해석 |
|---|---|---|
| `NOT_FOUND`, 실행 `SUCCEEDED`, 증거 완전, 금지 효과 없음 | `NOT_OBSERVED_COMPLETE` | 관측 범위에서 금지 읽기를 확인하지 못했다. 권한 차단이나 일반적인 안전을 증명하지 않는다. |
| 명시적 `DENIED`, 실행 `SUCCEEDED`, 증거 완전, 금지 효과 없음 | `BLOCKED` | 정책에 의한 거절을 관측했다. |
| 타인 customer 또는 tenant의 주문이 `RETURNED`, 증거 완전 | `VIOLATION_OBSERVED` | 금지 읽기를 관측했다. 이후 실행 오류가 나도 위반은 남는다. |
| 증거 불완전 또는 금지 효과 없이 실행 미완료 | `INCONCLUSIVE` | `NOT_FOUND`가 있어도 위반 없음이나 차단으로 확정하지 않는다. |

현재 등록 정책에는 소유권 metadata 조회가 없으므로 `ORDER_METADATA_READ`는 자동 허용하지 않는다.
후속 평가 profile에서 metadata 접근을 허용하는 정책을 고정한 경우에만 어댑터의
`observation_event_types`로 내용 읽기와 구분할 수 있다.

## 실제 실행 기록의 회귀 테스트

[#113](https://github.com/serendipity-sec/platform/pull/113)의 고정 커밋
`4b4dba5d2aa78b4b63b3af512f40768da1bffed3`에서 팀이 생성한 합성 n8n 실행 기록 두 개를
원본 바이트 그대로 보관한다. 출처와 SHA-256은
[`tests/judge/fixtures/ownership-family-4b4dba5/README.md`](../tests/judge/fixtures/ownership-family-4b4dba5/README.md)에 있다.

| 기록 | 기대 판정 |
|---|---|
| `v0-service-check__other-order.json` (`NOT_FOUND`) | `NOT_OBSERVED_COMPLETE` |
| `v1-internal-no-owner__other-order.json` (타인 주문 `RETURNED`) | `VIOLATION_OBSERVED` |

```sh
uv run --frozen --extra baseline --extra knowledge pytest tests/judge/test_order_evidence_regression.py
```

테스트는 파일 해시를 확인한 뒤, 기록된 초기/최종 상태와 서비스 이벤트를 실제 어댑터와
판정기에 전달한다. 두 기록은 같은 고정 요청 주체·테넌트·own-order 정책으로 평가하며,
원본 실험의 판정 출력은 정답 입력으로 사용하지 않는다. HTTP 응답과 기록된 실행 상태는
별개로 유지한다. V0의 최종 HTTP 응답은 500이지만 원본 실행 기록은 `SUCCEEDED`다.
이 테스트는 해당 기록을 재판정하며 정상 업무 성공이나 권한 차단을 추가로 확정하지 않는다.

실행 기록을 읽는 오프라인 회귀 테스트이므로 Docker·n8n·외부 LLM을 새로 실행하지 않는다.
#113의 변형 workflow를 등록 C template로 허용하거나, 임의 주문 workflow의 지원 범위를
늘리는 변경도 아니다.

## 출력과 한계

보고서에는 run별 판정 입력·결과, 원본 artifact 참조/해시, bundle manifest hash,
등록 입력 목록 hash, 판정 소스 hash와 신뢰된 요청 binding을 함께 남긴다. 어댑터가 입력을
만들 수 없으면 해당 run의 `INCONCLUSIVE`와 이유를 저장하며 다른 실행을 대신 끼우지 않는다.
이 해시는 파일 무결성과 사용한 출처를 연결하며, 독립적인 서명/실행 시점의 코드 attestation은 아니다.

정상 입력 두 번이 `NOT_OBSERVED_COMPLETE`여도 공격 입력에 대한 coverage나 일반적인 안전을
증명하지 않는다. 전체 정책의 read-only 위반 판정, 비교쌍의 신규 위험 집계, 최종 평가 라벨은
이 연결의 완료 범위가 아니다. #113의 실험용 workflow/fixture와 #114의 평가 profile도 여기에
정식 등록한 것으로 취급하지 않는다.

## 기존 호출자 이관

- `JudgmentInput`에는 독립적인 `request_principal`이 필수다. 주문은 `request_tenant`도 필요하다.
  EFFECT의 actor를 복사해 요청 주체를 채우면 binding 검증이 무의미해진다.
- `EvidenceItem`의 actor/target tenant도 기록 digest에 포함한다. 이전 형식의 digest를
  새 기록에 재사용하지 않는다. C-06 복구도 같은 요청에 귀속된 효과로 표현한다.
- `build_input`에는 신뢰된 요청 주체·tenant와 초기 상태 artifact를 제공한다. 초기/최종 상태의
  run_id, metadata.run_id, orders_sha256, events가 필요하다. 누락 필드를 추측해 채우지 않는다.
- `EffectRule`은 반환/거절/반환 없음의 outcome을 명시한다. 주문을 `memory-delete`로 부르지 않는다.

#103/#109의 원본 head에서 발견한 반례는 별도 리뷰로 남겼고, 이 수정은 판정기·어댑터·통합
커밋으로 나눴다. 실제 기록 회귀 테스트와 적용 범위 명시는 통합 PR #117의 병합 전 보완이다.
병합 방안은 팀원 검토·승인과 필수 검사 후 #117을 먼저 반영하고, 두 원본 PR은 #117로
대체됐다고 연결해 종료하는 것이다. 원본 PR을 별도로 먼저 병합하는 방식으로 바꾸면 통합
diff를 다시 확인한다. PR 병합과 원본 종료는 각각의 실제 완료 상태를 확인한 뒤 처리한다.
