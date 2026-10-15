# C 실제 실행 검증 — 2026-10-15

실행기 0.1.1을 계약 예시 계획 `plan-order-lookup-example`에 연결하여
실제 n8n 2.42.6에서 실행했다. A/B의 실제 생성물과 연결한 통합 실행과는 구분한다.

```powershell
docker build --tag flowdelta-order-fixture:0.3.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline flowdelta execute examples/contracts/order-lookup --out var/c-review-001
```

- comparison: `comparison-500cc6c4fd8d4c4a9e2bdecfcb0155de`
- before: HTTP 200, `{"order_id":"ORD-1001","status":"준비중"}`
- after: HTTP 200, 위 응답에 `"display_status":"주문 상태: 준비중"` 추가
- 양쪽 execution SUCCEEDED, collection COMPLETE, cleanup COMPLETE
- n8n image: `sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a`
- `runs/*.run.json`에 명시된 JSON만 복사했다. private/DB/상세 로그는 포함하지 않았다.
- `RUNTIME_OBSERVATION`과 `outcome=null`을 유지한다. 보안 탐지 성능의 증거는 아니다.

각 파일의 무결성은 run record의 SHA-256·크기와 비교할 수 있다.
새 실행은 새 출력 폴더와 새로운 run/comparison ID를 만든다.
