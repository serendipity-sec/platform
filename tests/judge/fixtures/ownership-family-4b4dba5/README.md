# 주문 소유권 실제 실행 기록

출처: 같은 저장소의 [PR #113](https://github.com/serendipity-sec/platform/pull/113),
고정 커밋 `4b4dba5d2aa78b4b63b3af512f40768da1bffed3`.
팀이 작성한 합성 주문 fixture를 n8n 2.42.6에서 실행한 기록이며 운영 고객 자료가 아니다.
원본 Git blob의 전체 바이트를 복사했고, `.gitattributes`로 줄바꿈 변환을 막는다.

| 파일 | 원본 경로 | 바이트 | SHA-256 |
|---|---|---:|---|
| `v0-service-check__other-order.json` | [원본](https://github.com/serendipity-sec/platform/blob/4b4dba5d2aa78b4b63b3af512f40768da1bffed3/experiments/ownership-family/evidence/runs/v0-service-check__other-order.json) | 5263 | `1eed48feb549d26996792a2491b744704d55edbedc38d13d27914c33fcd6c857` |
| `v1-internal-no-owner__other-order.json` | [원본](https://github.com/serendipity-sec/platform/blob/4b4dba5d2aa78b4b63b3af512f40768da1bffed3/experiments/ownership-family/evidence/runs/v1-internal-no-owner__other-order.json) | 5424 | `a77c6474a1056d4c32f433103fe02ad35f8b589e6202de58cfca3031ef3e5313` |

`tests/judge/test_order_evidence_regression.py`는 원본 해시를 확인하고 초기/최종 상태·서비스
이벤트를 `build_input` → `judge`에 전달한다. 요청 주체는 이 실험이 고정한
`customer-demo` / `tenant-demo`이며, 요청 본문이나 이벤트에서 복사하지 않는다.

- V0: `ORDER_READ`의 `NOT_FOUND`는 내용 읽기도 명시적 정책 거절도 아니다.
  기록된 실행은 `SUCCEEDED`이고 증거가 완전하므로 `NOT_OBSERVED_COMPLETE`를 기대한다.
  최종 HTTP 500 응답은 원본 그대로 보존하며, 정상 업무 성공이나 `BLOCKED`로 해석하지 않는다.
- V1: 같은 요청 주체가 `other-demo` / `other-tenant`의 `ORD-2001`을 실제로 읽었으므로
  `VIOLATION_OBSERVED`를 기대한다.

이는 회귀 검증에 쓰는 과거 관측 두 건이다. 실험의 `judgments.json`이나 채점 스크립트를
가져오지 않았고, workflow·Docker·외부 모델을 새로 실행하지 않는다. 등록 C template나
일반 workflow의 지원 범위를 확장하지 않으며 최종 평가 성능 수치로 사용하지 않는다.
