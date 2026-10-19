# 실제 A/B/C 통합 주문 조회 — 2026-10-20

팀 작성 정상 주문 조회 한 건을 전체 pipeline으로 두 번 실행한 증거다.
생성 코드 기준은 PR #38의 `7f4acbe`이며, 원본 실행은 각각 `var/integrated-order-001`,
`var/integrated-order-002`에 보관했다. 이 폴더에는 각 manifest가 명시한 JSON과
실행 요약만 복사했다. private DB·자격·상세 로그는 포함하지 않았다.

| 확인 사항 | 첫 실행 | 두 번째 실행 |
|---|---|---|
| A 실제 분석 | 변경 1건, MODULE_OUTPUT | 같은 analysis ID |
| B 실제 계획 | MODULE_OUTPUT | 같은 plan ID |
| C 전후 실행 | 모두 SUCCEEDED | 모두 SUCCEEDED |
| 자료 수집 / 정리 | 모두 COMPLETE / COMPLETE | 모두 COMPLETE / COMPLETE |
| 공유 artifact 수 | 42 | 42 |
| 전체 참조 검증 | CONTRACT_LINKS_VALID | CONTRACT_LINKS_VALID |
| 정상 업무 검사 | 전후 각 7개 통과 | 전후 각 7개 통과 |

- 분석: `analysis-5dfc5495950ceaca`
- 계획: `plan-67968e2d05c9fa987f094112` (`flowdelta.knowledge/1.1.0`)
- 첫 비교: `comparison-5f1afcdf143f4aacb9ebd502d7cee31f`
- 두 번째 비교: `comparison-a5d8551e9b2d43378ce4ac20708c3539`
- 네 run ID는 모두 다르다. 초기 이벤트는 매번 비어 있고, 주문 데이터는 실행 후에도 같다.
- 정상 요청은 `ORD-1001`이며 서비스가 각 실행에서 해당 주문을 한 번 조회했다.

변경 전 응답은 `{"order_id":"ORD-1001","status":"준비중"}`이다.
변경 후에는 `"display_status":"주문 상태: 준비중"`이 추가된다.
이 응답을 먼저 정해 둔 `runtime/order-lookup/expected.json`과 비교했다.

```powershell
uv run --frozen --extra baseline flowdelta check-pipeline examples/module-outputs/integrated-order-lookup/run-1/bundle
uv run --frozen --extra baseline flowdelta check-pipeline examples/module-outputs/integrated-order-lookup/run-2/bundle
```

전체 재현 방법은 [공통 작성기 안내](../../../docs/MODULE_BUNDLE.md)를 따른다.
확인한 범위는 한 정상 사례의 실제 연결·재현·격리다. 보안 outcome은 null이며
공격 생성, 위험 판정, 독립 보안 oracle, 실제 업무 데이터셋의 탐지율 평가는 남아 있다.
