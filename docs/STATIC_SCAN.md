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
- 규칙 0.6.0은 모델 입력이 `DELETE`·`PATCH`·`PUT` 요청의 URL에 영향을 주면 권한 검토가
  필요한 `WARNING`을 남긴다. 현재 입력 계약에는 인증된 요청자·역할 출처를 검증할 근거가 없다.
  다른 노드의 출력, fallback, 소유자 비교, `admin` 변수·함수명으로 finding을 숨기지 않는다.
  따라서 실제로 올바르게 인증·인가하는 코드에도 경고가 남을 수 있다. 신뢰 출처를 연결하는
  후속 계약과 검증 전까지는 검토가 필요한 범위로 보존한다. finding 부재도 권한 준수를 뜻하지
  않는다(`experiments/n8n-claw-c/README.md`).
- 지원 범위 밖의 노드는 건너뛰지 않고 `unsupported`에 이유와 함께 남긴다.

## 등록 template (`catalog.json`)

| 필드 | 값 | 뜻 |
|---|---|---|
| `template_id` / `version` | `static-agent-tool-ownership` / `0.1.0` | plan이 가리킬 이름 |
| `mode` | `STATIC_SCAN` | 정상 실행 template과 구분 |
| `tool` | semgrep `1.181.0`, 규칙 `0.6.0`, 규칙 파일 경로·SHA-256 | 같은 도구·규칙으로만 실행 |
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

## 실행 (`static_adapter`)

`src/flowdelta/runtime/static_adapter.py`가 plan을 받아 before·after를 같은 template으로 검사한다.
입력은 A가 분석한 handoff 폴더(`flowdelta snapshot` 출력), 공유 `TestPlan`, 위 정적 catalog다.

```bash
uv run --frozen --extra baseline --extra knowledge python -m flowdelta.runtime.static_cli \
  --handoff <handoff 폴더> --plan <plan.json> --catalog <static-scan-catalog.json> --out var/<새 폴더>
```

1. **검사 전 확인** (실패하면 아무 파일도 쓰지 않음): 위 plan 규칙, case·snapshot ID가 handoff와
   같음(`STATIC_CASE_ID_MISMATCH`, `STATIC_SNAPSHOT_ID_MISMATCH`), case의 obligation이 모두 bound
   또는 uncovered(`STATIC_OBLIGATION_SET_MISMATCH`), workflow 바이트가 handoff의 해시·크기와 같음
   (`ARTIFACT_INTEGRITY_MISMATCH`), 출력이 `var/` 아래 새 폴더(`OUTPUT_MUST_BE_UNDER_VAR`,
   `OUTPUT_ALREADY_EXISTS`).
2. **obligation**: 정적 검사는 `static-findings`·`static-raw-report`만 만든다. case obligation이 다른
   자료(예: `final-state`)를 요구하면 bind할 수 없고 uncovered로 둬야 한다
   (`STATIC_OBLIGATION_NOT_COVERABLE`). n8n-claw 사례의 `owner-scoped-memory-change`가 이 경우다.
   각 binding의 `required_artifact_kinds`는 해당 case의 `required_artifacts`와 정확히 같아야
   한다(`STATIC_OBLIGATION_EVIDENCE_MISMATCH`). 각 run에는 연결된 `obligation_ids`를 기록한다.
3. **대상 추출** (`static_targets.py`): agent에 `ai_tool`로 연결된 노드만 본다. 지원 노드의 코드는
   원본 JSON Pointer·코드 해시와 함께 `var/<out>/<side>/targets/`에 쓰고, 나머지는 이유와 함께
   `unsupported`에 남긴다(`TARGET_TYPE_NOT_SUPPORTED`, `AGENT_VERSION_NOT_SUPPORTED`,
   `CODE_PARAMETER_MISSING`).
4. **도구 실행**: 버전을 먼저 확인하고 side마다 한 번, plan의 `timeout_seconds`로 실행한다.
5. **결과**: `var/<out>/runs/<side>-1.static-run.json`. 원시 보고서는 같은 폴더의 `<side>/`에만 둔다.

| 상황 | status | error_code |
|---|---|---|
| 정상 | `SUCCEEDED` | — |
| 지원 대상이 없음 | `NOT_RUN` | `NO_SUPPORTED_TARGET` |
| 도구 없음 / 버전 다름 | `FAILED` | `STATIC_TOOL_NOT_FOUND` / `STATIC_TOOL_VERSION_MISMATCH` |
| 도구를 시작할 수 없음(권한 등) | `FAILED` | `STATIC_TOOL_START_FAILED` |
| 시간 초과 | `TIMED_OUT` | `STATIC_TOOL_TIMEOUT` |
| 도구 오류·비정상 종료 | `FAILED` | `STATIC_TOOL_ERROR` / `STATIC_TOOL_FAILED` |
| 보고서를 읽을 수 없거나 형식이 다름 / 다른 규칙 결과 | `FAILED` | `STATIC_RAW_REPORT_INVALID` / `STATIC_UNEXPECTED_RULE` |
| 추출한 파일을 도구가 읽지 않음 | `FAILED` | `STATIC_SCAN_INCOMPLETE` |
| 실행 전후 규칙 사본이 기록된 해시와 다름 | `FAILED` | `STATIC_RULE_CHANGED` |
| Ctrl+C (도구 버전 확인 중) | 두 side 모두 `INTERRUPTED` | `STATIC_INTERRUPTED` |
| Ctrl+C (side 검사 중) | 진행 중 side `INTERRUPTED`, 남은 side `NOT_RUN` | `STATIC_INTERRUPTED` / `PREVIOUS_SIDE_INTERRUPTED` |

side의 run 파일은 그 side가 끝나는 즉시 쓴다. 뒤 side에서 오류나 중단이 나도 앞 side 기록은 남는다.
검사하는 workflow는 1단계에서 해시를 확인한 바이트 그대로다(파일을 다시 읽지 않음). 규칙도 1단계에서
catalog의 `rule_sha256`과 같은지 확인한 바이트를 출력 폴더의 `rule/`에 사본으로 두고, before·after 모두
그 사본으로 실행한다. 실행 전후에 사본의 해시를 다시 확인한다. 출력 폴더를 만들
수 없거나 그 안에 쓸 수 없으면 도구 결과가 아니라 입력 오류 `STATIC_OUTPUT_WRITE_FAILED`로 멈춘다.

보고서는 JSON으로 읽히는 것만으로 받지 않는다. 최상위가 객체이고 `errors`·`results`·`paths.scanned`가
배열이며, 각 결과에 문자열 `check_id`·`path`, 1 이상의 정수 `start.line`, 알려진 `extra.severity`가 있고
결과가 추출한 파일을 가리켜야 한다. 하나라도 어긋나면 `STATIC_RAW_REPORT_INVALID`다(`results={}`,
`errors=null` 같은 보고서가 "경고 없음"으로 바뀌지 않게).
경로는 빈 문자열과 NUL 문자를 거부하며, 경로 해석의 `ValueError`·`OSError`도 같은 오류로
기록한다. 잘못된 보고서 때문에 side 기록 없이 중단하지 않고 before·after의 실패를 각각 남긴다.

`SUCCEEDED`가 아니면 finding은 비어 있다. 어떤 경우에도 "경고 없음"이 "안전"을 뜻하지 않는다.

아직 하지 않은 것: 결과의 공용 DB 저장·조회(`feat/51-result-repo`), B가 만든 실제 plan으로 다시
확인(공유 catalog 형식 합의 뒤, 아래 1번).

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
