# B: 로컬 카드 검색과 정상 계획 생성

이 구현은 계약 0.4.0의 카드와 등록 템플릿을 읽어 실행 계획을 만든다.
workflow나 카드에 적힌 코드·URL을 실행하지 않는다. 공개 함수는
`make_plan(handoff, analysis, cards, catalog) -> TestPlan`이다.

## 재현

로컬에 Python·uv를 설치하지 않고 프로젝트 전용 Docker 환경에서 검증하려면:

```bash
docker compose -f runtime/development/compose.yaml build check
docker compose -f runtime/development/compose.yaml run --rm check
```

컨테이너는 Python 3.13.15·uv 0.9.30·uv.lock을 사용한다. 전체 테스트, lint/format,
타입 검사, 두 계약 예제와 B CLI를 검증한다. 소스는 읽기 전용으로 연결하고
가상환경은 이미지 내부 `/opt/flowdelta-venv`, 임시 출력은 컨테이너 `/tmp`에 둔다.
검증 컨테이너는 네트워크를 사용하지 않는다. 의존성이 바뀌면 이미지를 다시 빌드한다.
기존 n8n 실행 실험과 Docker 기반 baseline/conformance는 이 검증 명령의 범위에 포함되지 않는다.

Python 3.13.15, uv 0.9.30과 잠긴 의존성을 사용한다.
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
3. node type이 지정됐지만 전후 workflow의 node type과 겹치지 않으면 제외한다.
4. 점수는 node type 교집합 수와 변경 필드 키워드의 제목/요약 일치 수의 합이다.
   업무는 catalog의 task_id로 확인한다. 변경 필드 전용 카드 속성은 추가하지 않는다.
5. 점수 내림차순, card ID와 version 오름차순으로 정렬하고 첫 카드의 템플릿을 선택한다.
6. 검색 결과가 없으면 업무/profile에 맞는 등록 정상 템플릿을 ID/version 순으로 선택한다.
   그런 템플릿도 없으면 `TEMPLATE_NOT_FOUND`다. 검색 결과 없음은 안전 판정이 아니다.

검색 방법은 `KEYWORD`, 모델 버전은 null이다. 알고리즘 버전은 query와 planner_metadata에
기록한다. 점수는 위험 확률이나 보안 성능이 아니다. node type 중첩은 후보 검색 근거이며
카드의 주장 전체가 적용된다는 증명은 아니다.

모든 obligation은 템플릿의 자료 종류로 연결하거나 uncovered 목록에 남긴다.
정상 입력과 초기 상태·snapshot 참조는 handoff에서 가져오고 `validate_pipeline()`으로 검사한다.
템플릿의 모든 실행 필수 자료는 C가 catalog에서 읽어 수집해야 한다.

## 출처와 인계

plan/retrieval은 실제 생성물이므로 MODULE_OUTPUT이다. 카드와 분석 예시의
CONTRACT_EXAMPLE은 바꾸지 않는다. 분석 출처는 출력 안내와 authorship note에 남긴다.
KB에는 정규 JSON 카드 내용과 그 정규 직렬화의 SHA-256을 담는다. 원본 파일의 공백까지
해싱한 값과는 다르며 `hash_basis=canonical-json-v1`로 구분한다.
retrieval은 KB 파일 bytes를, plan은 retrieval 파일 bytes를 참조한다.

A의 실제 분석이 도착하면 같은 analysis.json 자리에 연결하면 된다.
CLI의 analysis bundle에는 무결성 검사용 manifest.json과 input/이 필요하다.
C는 plan의 template ID/version, 정상 입력, 초기 상태, attempts/timeout을 소비한다.
전체 check-pipeline은 A 실제 생성물과 팀장 bundle 작성기가 연결된 뒤 수행한다.
현재 예시 분석으로 생성한 B 파일만으로 전체 실제 pipeline 완료를 주장하지 않는다.

테스트: `uv run --frozen --extra baseline pytest tests/knowledge`.
첫 구현에는 벡터 DB, 모델 API, 새로운 템플릿 생성이 포함되지 않는다.
