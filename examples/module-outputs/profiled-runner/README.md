# Profile 기반 runner의 등록 주문 조회 실행 요약

`summary.json`은 고정 profile → 입력 repository → 실제 A/B → 등록 C 전후 실행 → bundle
검사를 수행한 로컬 결과다. `verification.json`은 별도 읽기 검사로 기존
`runtime/order-lookup/expected.json`의 기대 응답, 초기 이벤트 없음, 주문 상태 불변,
실행 ID별 지정 주문의 RETURNED ORDER_READ 1건, 실행·수집·정리 완료를 확인한 요약이다.
검사 전 manifest·관측 해시·실행 계약과 요약의 실행 ID를 대조한다. 이벤트 문서 최상위의
실행 ID·수집 완료 상태와 run의 `FINISHED`·종료 시각도 확인한다.

실행 명령은 [PIPELINE_RUNNER.md](../../../docs/PIPELINE_RUNNER.md)를 따른다.
관측 파일 전체는 해당 명령의 `var/<output>/bundle`에서 재생성한다. 이 디렉터리는 선택된
요약만 포함하며 전체 bundle·원본 관측·private DB·자격을 재배포하지 않는다.
독립 읽기 검사는 다음과 같이 재실행한다. 결과는 새 파일에 쓴다.

```powershell
uv run --frozen --extra baseline --extra knowledge python tools/check_profiled_order.py var/pr87-registered-run --out var/pr87-verification.json
```

등록된 합성 주문 조회의 확인 결과다. n8n-claw 사례 실행·실제 agent 보안 위반·공용 DB
완료를 뜻하지 않으며 `security_outcome=null`이다. 2026-11-06에 main `a617a22`(#84, #61)와 #87의
공용 카드 검색·정적 계획 통합 `4b2a5ac` 코드로
등록된 Docker/n8n 2.42.6의 변경 전·후 주문 조회를 다시 실행했다. 두 실행의 실행·수집·정리가
완료됐고, 별도 읽기 검사와 `check-pipeline`을 통과했다. 비교 기준은 기존 2026-10-24 profile
(`order-lookup-review`, SHA-256 `2853bd8e455062f947af664ef90eb7f6f8c3edfa48eb9c0f685b531c39a1bce4`)
그대로 유지했다. 이 기록은 등록 정상 업무의 재검증이며 이전 실행과의 성능 비교가 아니다.

출처 해시는 `.gitattributes`가 정한 LF 바이트(저장소에 커밋된 내용)를 기준으로 한다.
줄바꿈이 CRLF로 바뀐 작업 트리에서 실행하면 `uv.lock` 등의 해시와 입력 profile 해시가
달라져 기존 비교 기준을 재현할 수 없고 회귀 테스트도 실패한다.

전체 Python 구현, runtime 설정, pyproject/uv.lock 등 127개 파일의 해시와 그 목록의
`source_tree_sha256`으로 실행한 소스를 식별한다. source 목록은 summary/관측 예제 자체를
포함하지 않는다. 회귀 테스트는 기록된 파일의 바이트뿐 아니라 현재 구현 파일 목록의 누락도
검사한다. 구현이 바뀌면 실제 실행·독립 검증을 다시 수행하여 두 JSON을 함께 갱신해야 한다.

이번 기록의 before는 `run-902680b6ff3c40b2b22ea58a7b98db4a`, after는
`run-769833e3c2344c0a86c3d5fe6442a7b7`이다. 정상 업무 검사 24개가 통과했고 두 실행 모두
수집·정리가 COMPLETE이다. 두 JSON은 실제 산출물을 바이트 그대로 복사했다.
정적 검사·결과 저장 코드가 source 목록에 포함되어도 이 주문 실행이 정적 검사나 n8n+LLM 실행을
수행했다는 뜻은 아니다. 그 범위의 검증은 별도 기록으로 구분한다.
