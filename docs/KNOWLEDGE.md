# B: 로컬 카드 검색과 정상 계획 생성

이 구현은 계약 0.4.0의 카드와 등록 템플릿을 읽어 실행 계획을 만든다.
workflow나 카드에 적힌 코드·URL을 실행하지 않는다. 공개 함수는
`make_plan(handoff, analysis, cards, catalog) -> TestPlan`이다.

검색과 계획 구성은 분리돼 있다. `compose_plan(handoff, analysis, cards, catalog, retrieval)`은
확정된 검색 결과를 받아 추가 검색 없이 계획을 만든다. analysis와 규칙 버전, 입력·catalog
fingerprint, 정규 카드 snapshot, retrieval ID와 결과 fingerprint, 카드 적격성과 hit/exclusion
분할·순위를 검사한다. `write_plan`은 검색을 한 번 수행하고
같은 결과를 계획과 `retrieval.json`에 사용한다. 기존 공개 함수와 CLI는 유지한다.

## 재현

로컬에 Python 3.13.15와 uv 0.9.30을 준비하고 잠긴 의존성을 사용한다.
출력 디렉터리는 미리 만들고, 세 출력 파일이 존재하지 않는 위치를 선택한다.

```bash
uv sync --frozen --extra baseline
mkdir -p var/b-first-plan
uv run --frozen --extra baseline flowdelta plan examples/contracts/order-lookup \
  --catalog examples/contracts/order-lookup/template-catalog.json \
  --cards examples/contracts/order-lookup/cards \
  --out var/b-first-plan/plan.json
```

출력은 `plan.json`, `retrieval.json`, `kb-snapshot.json`이다.
출력 디렉터리를 최종 bundle 루트로 취급하므로 이 세 파일은 번들 루트에 그대로 배치한다.
팀장 #20 작성기에 파일을 명시적으로 전달하고, input/analysis/catalog도 함께 포함한다.
기존 파일은 덮어쓰지 않는다. 파일 기록 실패 시 이 호출에서 만든 파일을 제거하고,
plan을 마지막에 기록한다. 강제 종료에 대한 원자적 디렉터리 게시까지 제공하지는 않는다.

## 선택 규칙

1. 카드는 ID와 version 쌍이 유일해야 한다. 외부 출처 참조는 전체 입력 bundle 루트에서 검증한다.
2. runtime profile과 등록 템플릿의 업무/profile이 맞지 않으면 제외한다.
   handoff의 정상 입력이 템플릿 입력 스키마와 맞지 않아도 제외한다.
3. node type이 지정됐지만 전후 workflow의 node type과 겹치지 않으면 제외한다.
4. 점수는 전후 workflow의 node type·변경 필드 일치와 A 영향 경로에 연결된 node type·필드
   일치를 합산한다. 영향 경로는 `change_id`에서 실제 before/after node type을 찾는다.
   `support`와 limitation은 점수를 안전 판정으로 바꾸지 않고 계획의 불확실성으로 보존한다.
   업무와 template은 catalog의 task_id로 확인한다.
5. 점수 내림차순, card ID와 version 오름차순으로 정렬하고 첫 카드의 템플릿을 선택한다.
6. 검색 결과가 없으면 업무/profile과 정상 입력에 맞는 등록 정상 템플릿을 ID/version 순으로 선택한다.
   업무/profile 후보가 없으면 `TEMPLATE_NOT_FOUND`, 후보가 모두 입력과 맞지 않으면
   `TEMPLATE_INPUT_INVALID`다. 검색 결과 없음은 안전 판정이 아니다.

검색 방법은 `KEYWORD`, 모델 버전은 null이다. 알고리즘 버전은
`flowdelta.knowledge/1.2.0`으로 query와 planner_metadata에 기록한다. retrieval과 plan에는
case/snapshot/analysis ID, 분석 생산자와 정규 JSON 해시, 영향 path·edge·change 출처,
path support·limitations, `flowdelta.graph.limits`를 보존한다. 점수는 위험 확률이나 보안
성능이 아니다. node type 중첩은 후보 검색 근거이며 카드의 주장 전체가 적용된다는 증명은 아니다.

모든 obligation은 템플릿의 자료 종류로 연결하거나 uncovered 목록에 남긴다.
정상 입력과 초기 상태·snapshot 참조는 handoff에서 가져오고 `validate_pipeline()`으로 검사한다.
템플릿의 모든 실행 필수 자료는 C가 catalog에서 읽어 수집해야 한다.

## 출처와 인계

plan/retrieval은 실제 생성물이므로 MODULE_OUTPUT이다. 카드와 분석 예시의
CONTRACT_EXAMPLE은 바꾸지 않는다. 분석 출처는 출력 안내와 authorship note에 남긴다.
KB에는 정규 JSON 카드 내용과 그 정규 직렬화의 SHA-256을 담는다. 원본 파일의 공백까지
해싱한 값과는 다르며 `hash_basis=canonical-json-v1`로 구분한다.
retrieval은 KB 파일 bytes를, plan은 retrieval 파일 bytes를 참조한다.
생성 KB가 8 MiB를 넘으면 `KNOWLEDGE_SNAPSHOT_SIZE_LIMIT`으로 기록 전에 거부한다.
같은 입력과 같은 생성 규칙은 같은 ID를 만들며, 생성 규칙 버전이 바뀌면 ID도 달라진다.

A의 실제 분석이 도착하면 같은 analysis.json 자리에 연결하면 된다.
CLI의 analysis bundle에는 무결성 검사용 manifest.json과 input/이 필요하다.
C는 plan의 template ID/version, 정상 입력, 초기 상태, attempts/timeout을 소비한다.
전체 check-pipeline은 A 실제 생성물과 팀장 bundle 작성기가 연결된 뒤 수행한다.
현재 예시 분석으로 생성한 B 파일만으로 전체 실제 pipeline 완료를 주장하지 않는다.

테스트: `uv run --frozen --extra baseline pytest tests/knowledge`.
전체 코드 검증은 `uv run --frozen --extra baseline sh tools/check-development.sh`로 실행한다.
스크립트는 Python·uv 지정 버전, lint/format, 타입, 전체 테스트, 두 계약 예제와 B CLI를 확인한다.
첫 구현에는 벡터 DB, 모델 API, 새로운 템플릿 생성이 포함되지 않는다.
