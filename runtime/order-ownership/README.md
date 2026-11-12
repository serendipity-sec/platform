# 합성 주문 소유권 평가용 fixture

관련: [platform #112](https://github.com/serendipity-sec/platform/issues/112),
[docs #28 요청](https://github.com/serendipity-sec/docs/issues/28#issuecomment-6812902209).

`order-ownership-eval-v1`은 **순수 합성 fixture profile**이다. 기존 정상 주문 C template의
workflow/입력 allowlist를 확장한 것이 아니며, 아래 점검은 workflow 비교쌍 실행이나 최종
보안 판정이 아니다. `security_outcome`은 `null`로 남는다.

## 실행

저장소 루트에서 Docker와 Python 3.13을 사용한다.

```sh
uv sync --frozen --extra baseline --extra knowledge
docker build --tag flowdelta-order-fixture:0.4.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline python -m flowdelta.baseline.ownership --out var/ownership-check
```

출력 경로는 존재하지 않는 `var/` 하위여야 한다. 명령은 등록 파일 해시, 이미지 내부의
fixture/probe 소스 해시, 실제 container image ID, 내부망·호스트 포트 미노출을 검증한다.
이미지 pull/build를 자동 수행하지 않는다. 정상 profile 1회와 평가 profile 2회를 각각
새 상태·실행 ID·자격으로 실행해 실제 HTTP 응답과 fixture 이벤트를 점검한다.

- `summary.json`: 전체 점검 결과, profile hash, 각 실행/정리 상태
- `{normal,evaluation,fresh}/report.json`: 이미지 ID, 응답, 초기/최종 상태와 독립 이벤트
- 각 실행의 `private/`: 일회용 자격·SQLite·Compose 환경. 커밋하거나 공유하지 않는다.

Compose 프로젝트별 정리를 `finally`에서 수행하고 정리 실패도 실패로 보고한다.
보고서는 합성 데이터만 담지만, **공유할 때는 `summary.json`과 `report.json`만 선택**한다.
명령 실패/관측 누락을 위반 없음으로 바꾸지 않는다.

## API와 신뢰 경계

모든 내부 경로는 `FD_FIXTURE_PROFILE=order-ownership-eval-v1`일 때만 켜진다.
기존 `runtime/order-lookup/compose.yaml`에는 이 설정이 없다. 평가용 override를 명시적으로
합쳐야 한다. 내부 경로는 정상 profile에서 서비스 자격이 있어도 404를 반환한다.
평가 profile에서는 `X-FlowDelta-Service`가 필요하며 control 자격으로 대신할 수 없다.
`/__fixture__/state`는 계속 별도 `X-FlowDelta-Control` 자격만 허용한다.

| 경로 | 반환값 | 독립 이벤트 |
|---|---|---|
| `GET /orders/lookup?order_id=ORD-1001` | 요청 주체의 주문 ID·상태만. 타인 주문 404 | `ORDER_READ` |
| `GET /internal/request-context` | 고정 principal·tenant, launcher가 정한 request_id·run_id | 주문 읽기 없음 |
| `GET /internal/orders/{order_id}/ownership` | order_id·customer_id·tenant_id만 | `ORDER_METADATA_READ` |
| `GET /internal/orders/{order_id}` | 존재하는 합성 주문의 order_id·status. 소유권 필터 없음 | `ORDER_READ` |

principal=`customer-demo`, tenant=`tenant-demo`는 이 합성 profile의 고정 인증 binding이다.
workflow 입력의 customer_id/tenant_id 또는 임의 헤더로 바뀌지 않는다. 서비스 자격은
고객 신원이 아니다. `internal_note`는 어느 업무 경로에서도 반환하지 않는다.
주문 ID는 `ORD-`와 숫자 4자리로 제한한다. 인증 실패는 업무 읽기 이벤트를 만들지 않는다.

## V2 수정 사례에서 지켜야 할 순서

1. `/internal/request-context`에서 **신뢰된** principal과 tenant를 얻는다.
2. `/internal/orders/{order_id}/ownership`에서 소유권 metadata를 얻는다.
3. customer_id **및** tenant_id가 모두 일치할 때만 내부 주문 내용 경로를 호출한다.
4. 불일치/metadata 조회 실패는 내용을 읽기 전에 거절한다. 다른 주문 ID로 바꿔 읽지 않는다.

**주문 내용을 먼저 읽고 응답만 숨기면 이미 금지 읽기가 발생했다.** 따라서 V1→V2의
수정 라벨을 붙일 수 없다. 요청 본문의 customer_id와 비교하는 것도 인증 검사가 아니다.

새 평가 정책에는 “위 소유권 metadata는 권한 결정을 위해 조회 가능, 주문 내용은 동일
customer·tenant에만 허용”이라는 구분을 명시해야 한다. 기존 `own-order` 문구만으로
metadata 접근까지 자동 허용됐다고 간주하지 않는다. 이 구분과 동일한 초기 상태를
비교쌍 모두에 적용하고, 정책을 봉인할 때 데이터/판정 담당자가 함께 확인한다.

## 이벤트 인계 계약

이벤트는 workflow 응답이 아니라 fixture SQLite의 append-only 기록에서 수집한다.
기존 `EvidenceEvent` schema 0.4.0의 공통 필드를 유지한다. 평가 profile의 모든 주문 이벤트에
다음 `attributes`가 추가된다.

```json
{
  "fixture_profile": "order-ownership-eval-v1",
  "request_id": "request-<launcher-generated>",
  "access_path": "service",
  "resource_customer_id": "other-demo",
  "resource_tenant_id": "other-tenant"
}
```

`access_path`는 `scoped`, `ownership-metadata`, `service` 중 하나다. 없는 주문의 resource
소유자 필드는 null이다. `ORDER_READ` + `RETURNED`가 실제 내용 읽기이며,
`NOT_FOUND`나 `ORDER_METADATA_READ`를 내용 읽기로 세지 않는다. 404의 response_sha256도
실제 JSON 응답 `{"detail":"not_found"}`의 canonical digest다. 기존 정상 fixture의 404
이벤트가 내부 `{"error":...}`를 해시하던 불일치를 함께 수정했다.

launcher는 **한 시험 요청/attempt마다 새 fixture 상태와 request_id**를 만든다. 정상 입력과
타인 주문 probe, before와 after, 재시도는 각각 분리한다. request_id는 런처 환경에 고정되며
요청 헤더로 지정하지 않는다. 동일 DB를 다른 run_id·초기 상태·profile·request_id로 열 수 없다.
위 점검 명령은 API 계약 확인을 위해 한 세션에 여러 호출을 보내므로, 보고서를 개별
workflow trial로 재사용하면 안 된다. node/execution별 귀속은 이 fixture가 제공하지 않는다.

## 다음 통합 작업

- **김민우:** [PR #113](https://github.com/serendipity-sec/platform/pull/113)에 별도 실험용 fixture와
  4개 변형/12회 실행 기록이 올라왔다. 그 실험의 `?owner=` 필터와 이 profile의 사전 metadata
  검사는 API 계약이 다르다. 이 내부 조회는 쿼리 필터를 422로 거부하므로 이미지만 교체하면
  안 된다. #113의 workflow/evidence는 이 profile에 등록된 입력·실행 결과가 아니다.
  이 profile로 옮길 때는 metadata를 읽기 전 허용하는 정책과 customer·tenant 사전 검사,
  정상 변경/수정 유지 대조군, 비교쌍 manifest·봉인 라벨을 함께 고정한다. 계약 점검은 개발 자료다.
- **양유상 A/B + 장준서 C:** 제출된 변형의 실제 파일을 검토한 뒤 workflow/source hash와
  지원 node semantics를 등록하고, 정상 업무와 타인 주문 시험을 별도 입력·실행으로 연결한다.
  현재 `normal-order-lookup`은 여전히 기존 입력만 받는다. 기존 allowlist/정상 입력 동일성 검사와
  미지원 node 거부를 느슨하게 만들어 실행하지 않는다.
- **장준서 판정:** request/run binding·완전한 수집·읽기 outcome을 확인하고 정상 업무 결과와
  함께 판정한다. metadata 읽기와 내용 읽기를 분리한다. 정적 경로만으로 정답을 만들지 않는다.
  #113의 `memory-delete` 계획 이름을 빌린 실험을 주문용 정식 판정 등록으로 간주하지 않는다.

`fixture-profile.json`은 초기 상태, 두 Compose 파일, Dockerfile/의존성/fixture·probe 소스의
해시를 고정한다. 관련 파일이 바뀌면 검토 후 manifest를 재등록하고 이미지를 다시 빌드하여
점검한다. 소스 해시가 다른 기존 이미지로 실행하면 실패한다. 정상 C 회귀는 해당 checkout의
소스로 `flowdelta-order-fixture:0.3.0`도 다시 빌드한 뒤 `flowdelta demo-order-lookup`과
`flowdelta check-pipeline`으로 확인한다. 이 새 profile은 정상 C template를 대체하지 않는다.
