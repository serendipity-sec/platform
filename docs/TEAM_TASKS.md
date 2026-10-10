# 4인 구현 착수 카드

2026-10-11 · [팀장 기준 환경](LEADER_BASELINE.md)에서 이어지는 **별도 역할·상세 작업 문서**

네 명 모두 구현을 맡는다. A/B/C는 역할 표기이며 실명·개인 마감은 팀장이 정한다.
기존 전체 설계·평가 계획을 대체하지 않는다. 날짜를 임의의 D1로 지정하지 않는다.

## 1. 오늘 배분할 단위

| 담당 | 소유할 구현 | 처음 사용할 입력 | 첫 PR의 결과 |
|---|---|---|---|
| 팀장 | 입력·정규화·의미 어댑터 확장 | source bundle, engine/node metadata | 추가 지원 조합과 지원 범위의 근거 |
| A | 그래프·노드 대응·변경/영향 | 완성된 input handoff | 실제 생성한 analysis.json |
| B | 지식 카드·로더·검색·template 선택 | 수작업 analysis, cards, catalog | 실제 생성한 plan.json |
| C | 계획 실행·상태 분리·초기화·기록 | 수작업 plan, catalog, 동작하는 baseline | plan을 읽어 만든 before/after 실행 파일 |

공통 서명은 [interfaces.py](../src/flowdelta/interfaces.py)에 있다. Protocol은 구현 약속이며
아직 동작하는 A/B/C 구현 클래스가 아니다. 각 구현자는 자기 코드의 테스트도 작성한다.

## 2. A — A-START: 그래프와 변경 추출

### 수정 위치와 공개 함수

- 새 폴더: `src/flowdelta/graph/`, `tests/graph/`.
- 공개 함수: `analyze(handoff: Handoff) -> AnalysisBundle`.
- CLI: `flowdelta analyze <handoff-dir> --out <new-analysis-file>`를 추가한다.
- 입력 파일 로더는 `pipeline_io.read_handoff()`를 재사용한다.

### 입력과 기대 출력

- 입력: `examples/contracts/order-lookup/input/`.
- 기대 출력: `examples/contracts/order-lookup/analysis.json`.
- 동일한 노드 ID 3개를 대응시키고 control 연결 2개를 양쪽에 만든다.
- `response`의 `/nodes/2/parameters/responseBody` 변경 한 개를 추출한다.
- `Change.before_source/after_source`에 원본 artifact ID와 JSON Pointer를 넣는다.
- 실제 생성물은 `authorship.origin=MODULE_OUTPUT`, producer는 자신의 모듈/버전으로 표시한다.

### 구현 순서

1. normalized node/connection에서 포트와 방향을 보존해 control graph를 생성한다.
2. 동일 ID를 우선 대응한다. 한쪽에만 있으면 ADDED/REMOVED. 중복·모호한 대응을 추측하지 않는다.
3. parameter·표시 필드·연결 변경을 구분한다. 원본 값과 위치를 남긴다.
4. case의 필수 obligation ID와 task/policy 참조를 그대로 전달한다.
5. 다음 PR에서 의미 어댑터의 reads/writes/effects를 이용한 관계·영향 범위를 추가한다.

### 오류와 완료 조건

- 입력 해시 불일치·dangling node·중복 ID는 안정적인 오류 코드로 반환한다.
- `UNKNOWN/UNSUPPORTED`에서는 확인하지 않은 효과를 확정하지 않고 limitations에 남긴다.
- 좌표만 변경한 입력은 DISPLAY_CHANGED로 구분하며 실제 업무 효과가 바뀌었다고 추정하지 않는다.
- 첫 완료: 정상 예제의 실제 생성 결과가 위 기대 구조와 일치하고 `validate_pipeline()`에서 연결된다.
- schema를 하드코딩한 JSON 복사로 결과를 만들지 않는다. 최소 다른 ID·parameter의 작은 입력에서도 생성되는지 확인한다.

**A가 지금 기다릴 팀원 산출물: 없음.** input과 의미 파일은 준비됐다.

## 3. B — B-START: 카드와 정상 계획

### 수정 위치와 공개 함수

- 새 폴더: `src/flowdelta/knowledge/`, `tests/knowledge/`.
- 공개 함수: `make_plan(handoff, analysis, cards, catalog) -> TestPlan`.
- CLI: `flowdelta plan <analysis-bundle> --catalog <file> --cards <dir> --out <new-file>`.
- 첫 단계는 파일 카드·결정적인 검색/선택으로 구현한다. 모델/API key와 벡터 DB 없이 시작 가능하다.

### 입력과 기대 출력

- 입력: 위 input, `analysis.json`, `cards/*.json`, `template-catalog.json`.
- 기대 출력: `examples/contracts/order-lookup/plan.json`.
- 변경 노드 종류·필드·업무에 맞는 두 카드를 로드한다.
- 등록된 `normal-order-lookup@1.0.0`을 고른다.
- case/snapshot/analysis/catalog ID, 정상 입력, 초기 상태 참조를 연결한다.
- 필수 obligation을 유지하고 각 항목의 필요한 자료 종류를 연결한다.

### 구현 순서

1. `ScenarioCard`로 파일을 읽고 중복 card ID/버전·잘못된 참조를 거부한다.
2. profile·node type·업무·변경 필드로 검색하고, 동점 처리와 검색 근거를 일정하게 유지한다.
3. 검색한 card의 template ID/버전을 catalog와 대조한다.
4. `TestPlan`을 생성하고 `validate_pipeline()`을 통과시킨다.
5. 다음 PR에서 검색 점수/근거·미지원 처리·선택 비교를 확장한다. 이후 RAG 실험을 연결한다.

### 오류와 완료 조건

- template가 없거나 버전이 다르면 `TEMPLATE_NOT_FOUND`로 반환한다.
- 필요한 항목을 연결할 수 없으면 `uncovered_obligation_ids`에 남긴다. 빈 계획을 성공으로 가장하지 않는다.
- catalog에 없는 경로·실행 코드·외부 대상을 만들어 넣지 않는다.
- 카드 내용은 참고 데이터다. 코드나 작업 지시로 실행하지 않는다.
- 첫 완료: 분석 예시에서 자동 plan을 생성하고, 검색 결과가 비어 있을 때도 원인을 설명하며 필수 항목을 보존한다.

**B가 지금 기다릴 팀원 산출물: 없음.** 분석 예시와 catalog는 팀장 제공본으로 시작한다.

## 4. C — C-START: 계획을 처리하는 실행 엔진

### 수정 위치와 공개 함수

- 새 폴더: `src/flowdelta/runtime/`, `tests/runtime/`.
- 공개 함수: `execute_plan(handoff, plan, catalog, output: Path) -> list[ExecutionRun]`.
- CLI: `flowdelta execute <plan-bundle> --out <fresh-run-dir>`.
- 기반: `runtime/order-lookup/compose.yaml`, `baseline/fixture.py`, `baseline/manual.py`.
- baseline은 참고 가능한 실제 실행 코드다. C의 엔진은 plan과 template를 읽어 수명주기를 관리하도록 분리한다.

### 입력과 기대 출력

- 입력: 위 input, `plan.json`, catalog, runtime lock.
- 파일 형식 예시: `run.example.json`. 실제 실행 자료: `runtime/order-lookup/evidence/`.
- before/after마다 새 run ID, DB, 자격, Compose project를 사용한다.
- 출력: run.json, response, initial-state, final-state, events, runtime, cleanup 자료.
- 파일 해시·크기·자료 종류를 실제로 존재하는 ArtifactRef와 연결한다.

### 구현 순서

1. side 선택과 고정 template dispatch를 분리한다. 먼저 정상 주문 template 한 개만 구현한다.
2. plan의 case/snapshot/template/초기 상태·timeout·필수 항목을 실행 전에 확인한다.
3. 준비 → 실제 요청 → 수집 → 종료 단계를 `ExecutionRun.phase`로 기록한다.
4. 정상 종료와 요청/수집 실패에서 자원 정리를 실행하고, 정리 실패도 보존한다.
5. 새 실행을 기존 실행 DB에서 시작하지 않는 초기화 경로를 만든다.
6. 다음 PR에서 등록된 정상 template를 늘리고, 중단/재시도/동시 실행의 상태 분리를 추가한다.

### 오류와 완료 조건

- 입력/lock 불일치·미지원 template·필수 항목 미포함 상태에서는 요청을 실행하지 않는다.
- 업무 성공, 자료 수집, 정리 성공은 독립 상태다. 자료가 없으면 `PARTIAL`/missing 목록을 남긴다.
- workflow 응답을 보안상 PASS로 바꾸지 않는다. 현재 outcome은 항상 null이다.
- 첫 완료: 제공된 plan을 읽고 before/after를 실행한 다음 새 폴더로 재실행해 같은 업무 결과를 얻는다.
- 실행 간 run ID·DB·이벤트가 섞이지 않고 종료 후 해당 프로젝트의 자원이 남지 않아야 한다.
- 첫 엔진은 등록된 로컬 정상 template만 처리한다. 임의의 workflow/URL/명령 실행 기능을 추가하지 않는다.

**C가 지금 기다릴 팀원 산출물: 없음.** 기준 환경·plan·catalog·실제 정상 결과가 준비됐다.

## 5. 팀장 — L-NEXT: 의미 범위를 넓히고 공통 형식을 관리

- 소유: `contracts.py`, `normalize.py`, `semantics.py`, `expressions.py`, input 예제·Schema.
- 첫 세 노드는 등록된 parameter 조합만 지원한다. 추가 조합은 읽기·쓰기·효과와 원본 위치를 먼저 정의한다.
- node metadata와 해당 정상 실행을 함께 확인한 뒤 지원 범위를 넓힌다.
- 두 번째 업무 후보의 원본·이용 조건·입출력·실제 환경과의 차이를 정리한다.
- 공통 계약을 바꾸면 소비하는 A/B/C 파일, 예시, 테스트, Schema를 같은 PR에 반영한다.
- 실제 구현 이슈의 크기를 조정하고, 서로의 PR을 소비자 관점에서 읽도록 연결한다.

의미 해시·하위 workflow·복잡한 분기/반복 등은 첫 연결이 동작한 뒤 순서를 정한다.

## 6. 합치는 순서와 완료 기준

| 단계 | 목표 | 다음 단계로 갈 조건 |
|---|---|---|
| 착수 | 같은 입력과 계약으로 각자 개발 | 세 명 모두 자기 입력을 읽고 오류 예시를 설명할 수 있음 |
| 첫 출력 | A analysis / B plan / C run 파일 | 수작업 예시와 실제 모듈 생성물이 구분됨 |
| 첫 연결 | 팀장→A→B→C 자동 흐름 | 중간 파일 ID·hash·필수 항목 연결, 정상 결과 재현 |
| 확장 | 두 번째 업무와 추가 노드 조합 | profile/미지원 범위를 유지하면서 같은 공통 코어 사용 |
| 연구 연결 | 고정 데이터셋·비교군 실행 | 정상 업무 보존, 관측 가능 비율, 미지원, 비용을 함께 측정 |

첫 연결에서는 수작업 analysis 대신 A의 출력, 수작업 plan 대신 B의 출력을 순서대로 바꾼다.
실패 시 마지막으로 통과한 파일과 오류 코드를 남겨 어느 경계에서 끊겼는지 찾는다.

## 7. 팀장이 오늘 직접 할 결정

1. A/B/C에 팀원 실명을 붙인다.
2. 각자 가용 시간을 확인하고 **첫 PR 제출일**을 합의한다.
3. 이 문서의 해당 카드를 팀원에게 전달한다. 각 구현 이슈를 만들고 브랜치를 이슈 번호와 연결한다.
4. 공통 입력 PR과 기준 환경 PR을 다른 팀원이 읽고 승인하도록 담당자를 정한다.
5. 첫 회의에서 `case → analysis → plan → run` 파일을 함께 열고 각자의 시작 입력을 확인한다.

작성자의 승인으로 main에 합치지 않는다. [기여 규칙](../CONTRIBUTING.md)의 peer approval을 따른다.
실명·개인 일정·리뷰어 지정은 아직 수행한 것으로 표시하지 않았다.
