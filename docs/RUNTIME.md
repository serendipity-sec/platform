# C: 계획에 따른 주문 조회 실행

`PlanExecutor.execute_plan()`은 B의 계획, 입력 handoff, 등록 catalog를 받아
각 side/attempt를 별도 Compose 프로젝트에서 실행한다. 현재 등록된 템플릿은
`normal-order-lookup@1.0.0` 하나다. 임의 workflow를 실행하는 일반 엔진은 아니다.

```powershell
uv sync --frozen --extra baseline
docker build --tag flowdelta-order-fixture:0.3.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline flowdelta execute examples/contracts/order-lookup --out var/c-run-001
```

위 명령은 계약 예시 계획으로 실행기를 확인한다. 실제 A/B 통합은 실제 생성물 bundle을
같은 명령에 전달한다. `python -m flowdelta.runtime`도 같은 실행 경로다.
n8n 이미지는 `runtime.lock.toml`의 digest가 로컬에 준비되어 있어야 한다.

실행 전에 case/snapshot/profile, 등록 template, 정상 입력, obligation의 누락 여부,
필수 자료 종류, 초기 상태·workflow의 승인된 해시를 검사한다. 실패하면 Docker 명령을
시작하지 않는다. 계획의 timeout은 실제 HTTP observer에 전달한다. Docker exec를
시작하고 종료하는 데 별도 15초 여유를 주지만 HTTP 요청의 제한은 늘리지 않는다.

`runs/<side>-<attempt>.run.json`과 그 안의 ArtifactRef가 공유 가능한 JSON 증거다.
필수 자료는 initial-state, response, final-state, events다. 실제 엔진·이미지·fixture 소스,
실행별 ID, 정리 후 해당 프로젝트의 잔여 리소스도 기록한다. 상세 명령 로그·DB·자격은
`private/`에만 보관하며 공통 JSON bundle에 넣지 않는다.

실행 SUCCEEDED, 수집 COMPLETE, 정리 COMPLETE는 각각 확인해야 한다.
SUCCEEDED는 등록 정상 요청의 HTTP 성공이며 보안 검증 성공을 뜻하지 않는다.
실행이 실패하거나 제한 시간을 넘겨도 수집·정리를 시도하고 별도 상태를 남긴다.
`authorship.origin=RUNTIME_OBSERVATION`, 보안 `outcome=null`을 유지한다.
실제 업무 기대 응답과 상태 불변성은 통합 재현 결과에서 별도로 확인한다.

```powershell
uv run --frozen --extra baseline pytest tests/runtime
```

단위 테스트의 FakeDocker는 실패·타임아웃·정리 분기를 확인하며 실제 n8n 실행의 증거를
대신하지 않는다. 실제 실행에는 별도의 새 출력 경로를 사용한다.
