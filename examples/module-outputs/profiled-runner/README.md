# Profile 기반 runner의 등록 주문 조회 실행 요약

`summary.json`은 고정 profile → 입력 repository → 실제 A/B → 등록 C 전후 실행 → bundle
검사를 수행한 로컬 결과다. `verification.json`은 별도 읽기 검사로 기존
`runtime/order-lookup/expected.json`의 기대 응답, 초기 이벤트 없음, 주문 상태 불변,
실행 ID별 ORDER_READ 1건, 실행·수집·정리 완료를 확인한 요약이다.

실행 명령은 [PIPELINE_RUNNER.md](../../../docs/PIPELINE_RUNNER.md)를 따른다.
관측 파일 전체는 해당 명령의 `var/<output>/bundle`에서 재생성한다. 이 디렉터리는 선택된
요약만 포함하며 전체 bundle·원본 관측·private DB·자격을 재배포하지 않는다.
독립 읽기 검사는 다음과 같이 재실행한다. 결과는 새 파일에 쓴다.

```powershell
python tools/check_profiled_order.py var/pipeline-execute --out var/normal-checks.json
```

등록된 합성 주문 조회의 확인 결과다. n8n-claw 사례 실행·실제 agent 보안 위반·공용 DB
완료를 뜻하지 않으며 `security_outcome=null`이다. 출판 전 실행한 소스의 주요 파일 해시는
summary에 보존했다. 아직 commit하지 않은 소스를 실행한 결과이므로 commit SHA 대신 파일
해시로 해당 구현을 식별한다.
