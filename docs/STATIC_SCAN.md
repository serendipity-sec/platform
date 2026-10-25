# C 정적 검사 계약 (제안)

Refs #51. C가 B(#50)에 먼저 제공하는 정적 검사 catalog와 plan·결과 형식이다.
코드는 `src/flowdelta/runtime/static_scan.py`, 예시는 `examples/static-scan/`,
JSON Schema는 `schemas/static-scan-0.4.0.schema.json`이다.

**공유 계약은 바꾸지 않았다.** `pipeline_contracts.py`의 `Template.mode`는 정상 실행 두 가지뿐이고
`ExecutionRun`은 실행 전용 필드로 되어 있다. 여기에 정적 검사를 넣는 방식은 #48·#50과 합의한 뒤
반영한다(#51 설계 검토 3-1). 그때까지 C 영역의 별도 계약으로 두며, 공유 `TemplateCatalog`가 이
template을 받지 않는다는 점도 테스트로 고정했다.

## 정적 검사가 하는 것과 하지 않는 것

- workflow 파일만 읽는다. n8n을 띄우거나 대상 코드를 실행하거나 요청을 보내지 않는다
  (`executes_target_code: false`, `network: NONE`).
- 결과는 `STATIC_CONDITION`이다. 실제 정책 위반 효과가 아니며 `outcome`은 항상 `null`이다.
- 지원 범위 밖의 노드는 건너뛰지 않고 `unsupported`에 이유와 함께 남긴다.

## 등록 template (`catalog.json`)

| 필드 | 값 | 뜻 |
|---|---|---|
| `template_id` / `version` | `static-agent-tool-ownership` / `0.1.0` | plan이 가리킬 이름 |
| `mode` | `STATIC_SCAN` | 정상 실행 template과 구분 |
| `tool` | semgrep `1.181.0`, 규칙 `0.3.0`, 규칙 파일 경로·SHA-256 | 같은 도구·규칙으로만 실행 |
| `targets` | `toolCode@1.2`의 `jsCode`, `ai_tool`로 `agent@3.1`에 연결된 것 | 허용 입력 |
| `result_kinds` | `static-findings`, `static-raw-report` | obligation이 요구할 수 있는 자료 |
| `timeout_seconds` | 120 | plan의 상한 |

catalog는 등록표에서 생성한다. 규칙 파일이 생성본과 다르면 `STATIC_RULE_MISMATCH`, catalog의
template이 등록표와 다르면(예: 규칙 해시 변경) `STATIC_TEMPLATE_NOT_REGISTERED`다.

## plan 규칙 (`validate_static_plan`)

지금은 기존 `TestPlan` 형식을 그대로 받는다. 검사 전에 아래를 확인하며 실패하면 아무 파일도 읽지 않는다.

| 조건 | 오류 코드 |
|---|---|
| `catalog_id`가 catalog와 다름 | `STATIC_CATALOG_ID_MISMATCH` |
| `template_id`·`template_version`이 catalog에 없음 | `STATIC_TEMPLATE_NOT_FOUND` |
| catalog의 template이 등록표와 다름 | `STATIC_TEMPLATE_NOT_REGISTERED` |
| `normal_input`이 비어 있지 않음 | `STATIC_NORMAL_INPUT_NOT_ALLOWED` |
| `attempts_per_side`가 1이 아님 | `STATIC_ATTEMPTS_INVALID` |
| `timeout_seconds`가 template보다 큼 | `STATIC_TIMEOUT_EXCEEDED` |
| obligation이 정적 검사로 만들 수 없는 자료(응답·상태·이벤트)를 요구 | `STATIC_OBLIGATION_UNSUPPORTED` |

유효 예시는 `plan.json`, 무효 예시 6개와 기대 오류는 `invalid-plans.json`이다.

## 결과 (`runs/*.static-run.json`)

side마다 하나. `workflow_sha256`, 도구·규칙 버전, `scanned_node_ids`, `unsupported`, `findings`,
`status`, 원시 보고서 참조를 남긴다.

- `status`: `NOT_RUN`, `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `INTERRUPTED`
- `SUCCEEDED`만 `findings`를 가질 수 있고 원시 보고서·시작/종료 시각이 필요하다.
  실패·timeout·중단은 `error_code`가 필요하며 경고 목록을 비워 둔다. 즉 실패가 "경고 없음"으로
  보이지 않는다.
- finding: 규칙 ID·버전, 노드 ID·이름·종류·버전, 원본 workflow의 JSON Pointer, 코드 SHA-256,
  jsCode 안의 줄 번호. 원본 코드 문자열은 넣지 않는다.
- 원시 보고서에는 원본 코드가 들어 있어 `var/`에만 두고 해시·크기로 참조한다.

예시의 finding 값은 `experiments/n8n-claw-c`의 실제 실행 결과와 같다. 예시 자체의
`authorship.origin`은 `CONTRACT_EXAMPLE`이다.

## 합의가 필요한 것

1. **공유 계약에 넣는 방법**: `Template.mode`에 `STATIC_SCAN`을 더할지, 정적 catalog를 별도로
   둘지. 결과는 `ExecutionRun`과 분리하는 안을 제안한다(#48, #50).
2. **template과 업무의 결합**: 공유 `validate_pipeline`은 template의 `task_id`·`runtime_profile`이
   case와 같아야 한다. 정적 검사는 업무와 무관하게 쓸 수 있어 이 두 필드를 두지 않았다(#48, #50).
3. **plan 형식**: 정적 검사는 `normal_input`·`initial_state_ref`가 필요 없지만 `TestPlan`에서
   필수다. 그대로 둘지, 정적 plan을 따로 둘지(#50).
4. **실행 위치**: #59 runner는 C 단계 전체를 `--execute` 뒤에 둔다. 대상 코드를 실행하지 않는
   정적 검사도 같은 조건을 따를지(#48).

## 재생성

계약을 바꾼 뒤 규칙·예시·schema를 다시 만든다. 테스트가 커밋된 파일과 생성 결과를 비교한다.

```bash
uv run --frozen --extra baseline --extra knowledge python tools/refresh_static_examples.py
```
