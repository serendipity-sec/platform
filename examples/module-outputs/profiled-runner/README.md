# Profile 기반 runner의 등록 주문 조회 실행 요약

`summary.json`은 고정 profile → 입력 repository → 실제 A/B → 등록 C 전후 실행 → bundle
검사를 수행한 로컬 결과다. `verification.json`은 별도 읽기 검사로 기존
`runtime/order-lookup/expected.json`의 기대 응답, 초기 이벤트 없음, 주문 상태 불변,
실행 ID별 지정 주문의 RETURNED ORDER_READ 1건, 실행·수집·정리 완료를 확인한 요약이다.
검사 전 manifest·관측 해시·실행 계약과 요약의 실행 ID를 대조한다.

## 2026-10-28 #84 / #59 통합 코드 재실행

#84 정책 binding 브랜치에 #59 `ea608a6a4c5d58d8c45f25939f35f481e445c0dc`를 통합한
`19d0ad48269b5bda4e52742779af9f5589b6ed03`의 실제 코드로 Docker/n8n 2.42.6을 실행했다.
이 commit은 main `4396e93`(#69 포함)과 #58 `f677919`도 포함한다.
#59 브랜치의 과거 기록은 그대로 남기고 이 정책 브랜치의 예제만 새 실행으로 갱신했다.
두 JSON은 실제 실행·독립 검증 산출물과 바이트 단위로 같다. source_hashes를 수동 수정하지 않았다.

| side | run_id | 실행 | 수집 | 정리 |
|---|---|---|---|---|
| before | `run-952a812dc5d444f0a3ee3cdd3fa5b9db` | SUCCEEDED | COMPLETE | COMPLETE |
| after | `run-cd3f411b28924d4fb639b99f0df10377` | SUCCEEDED | COMPLETE | COMPLETE |

독립 정상 업무 검사 **18/18 통과**, `check-pipeline`은 CONTRACT_LINKS_VALID이다.
기존 profile `order-lookup-review` / revision `2026-10-24`의 선언을 복원한 뒤
기록된 SHA-256 `2853bd8e455062f947af664ef90eb7f6f8c3edfa48eb9c0f685b531c39a1bce4`과 정확히 일치함을 확인했다.
기존 expected.json도 그대로 사용했다(SHA-256 `36a79fc4cd6e93f2d32c0e6393557a6da3b27277f12acf5c1c40446b72dd1cde`).

## 증거와 재현

실제 실행: `var/pr84-stack-order-20261028/`.
독립 읽기 결과: `var/pr84-stack-order-20261028-verification.json`.

```powershell
uv run --frozen --extra baseline --extra knowledge flowdelta run-pipeline examples/order-lookup --profile var/review-20261024-profile.json --profile-sha256 2853bd8e455062f947af664ef90eb7f6f8c3edfa48eb9c0f685b531c39a1bce4 --catalog examples/contracts/order-lookup/template-catalog.json --cards examples/contracts/order-lookup/cards --out var/pr84-stack-order-20261028 --execute
uv run --frozen --extra baseline --extra knowledge python tools/check_profiled_order.py var/pr84-stack-order-20261028 --out var/pr84-stack-order-20261028-verification.json
uv run --frozen --extra baseline --extra knowledge flowdelta check-pipeline var/pr84-stack-order-20261028/bundle
```

출력 폴더가 이미 있으면 새 이름으로 실행한다. 일반 사용법은
[PIPELINE_RUNNER.md](../../../docs/PIPELINE_RUNNER.md)를 따른다.
이 디렉터리는 선택된 요약만 포함하며 전체 bundle·원본 관측·private DB·자격을 재배포하지 않는다.

- 전체 Python 구현·runtime·dependency 파일 **111개**의 해시가 Git blob bytes와 일치.
- source_tree_sha256: `9d95c8ba67638857c671c427b41229af17a5889a46a0f47b91cf048f2325e380`
- bundle_manifest_sha256: `cf8af89cc068215ca3ef3fba7aae3d01327baacd49b9e9bb644fc6b6322a1625`
- summary_sha256: `fed10e7a5d781c277bcc1cb5e4a25cbdd636697f3ffb905e91fdd1ae3c169299`

source 목록은 summary/관측 예제 자체를 포함하지 않는다. 회귀 테스트는 기록된 파일 bytes와
현재 구현 파일 목록의 누락을 검사한다. 구현이 바뀌면 실제 실행·독립 검증을 다시 수행하여
두 JSON을 함께 갱신해야 한다.

## 검증 범위

등록된 **binding 없는 합성 정상 주문** 실행이며 `security_outcome=null`이다.
정책 코드가 source 목록에 포함돼도 이 실행이 #61의 analyze 자동 연결 또는 ownership 위반을
검증했다는 뜻은 아니다. #61 실제 모듈 조합은 [정책 인계 문서](../../../docs/POLICY_BINDINGS.md)의
별도 스크립트로 확인한다. 선정 n8n-claw 전체 agent/LLM·공용 DB 배포·독립 보안 판정은
이 정상 주문 실행의 범위 밖이다.
