# n8n-claw 소유권 누락 — C 정적 검사 도구 적합성

Refs #51. 대상 사례는 #48에서 선정하고 PR #57에 공유한 n8n-claw Memory Delete/Update의
소유권 확인 누락이다. 이 폴더는 **어떤 정적 검사 도구가 이 문제를 실제로 구분하는지**를 확인한
기록과 재현 절차다.

정적 탐지는 "정적 조건 성립"까지만 말한다. agent가 도구를 고르는지, 타인의 기록이 실제로
바뀌는지는 확인하지 않는다. PR #57의 도구 JS 부분 실행과도 별개이며 서로 대신하지 않는다.

## 대상과 비교쌍

원본은 `sources.json`의 고정 commit 7개다(PR #57 `888de5d`의 sources.json과 같은 commit·해시).
원본 저장소에 LICENSE가 없어 원본·추출 코드·원시 Semgrep 결과는 `var/`에만 둔다.

| 비교 | 쌍 | case_id(#64와 같음) | 기대 |
|---|---|---|---|
| 기능 추가 | `9ebf36b` → `63a7f2e` | `n8n-claw-memory-feature-add` | 추가 후 탐지 |
| 첫 보완 | `4d1b1b7` → `eb6d7bc` | `n8n-claw-memory-first-fix` | 보완 전 탐지, 보완 후 미탐지 |
| 되돌림 | `eb6d7bc` → `2c6df44` | — | 되돌린 뒤 다시 탐지 |
| 후속 보완 | `c71b39e` → `fe57d16` | — | 둘 다 미탐지 |

취약 지점: AI Agent(`agent@3.1`)에 `ai_tool`로 연결된 `toolCode@1.2`의 **Memory Delete와
Memory Update**. 모델이 넘긴 `id`로 서비스 키를 써서 `DELETE`/`PATCH`를 보내며, 보완 전에는
대상 행의 `owner_user_id`와 요청자를 비교하지 않는다. 원저자의 첫 보완도 두 도구에 함께 검사를 넣었다.

## 결과

판정 기준: Memory Delete/Update에 대한 결과가 **취약 버전과 보완 버전에서 달라야** 쓸 수 있다.
무관한 일반 경고는 탐지로 세지 않는다.

| 도구 | 공신력 | 기능 추가 후 새 도구 표시 | 소유권 누락 탐지 | 취약(`4d1b1b7`) vs 보완(`eb6d7bc`) |
|---|---|---|---|---|
| n8n audit (n8n 2.42.6) | n8n 공식 | 없음 | 없음 | 결과 동일 |
| Agentic Radar 0.14.1 | SplxAI(Zscaler) | 표시(ACE) | 없음 | 결과 동일 |
| FlowSentry 0.1.0 | 개인 | 없음 | 없음 | 결과 동일 |
| n8n-flowguard 0.1.1 | 개인 | 없음 | 없음 | 결과 동일 |
| **Semgrep 1.181.0 + 팀 규칙 0.4.0** | Semgrep 엔진 | 표시 | **Delete·Update** | **구분됨** |

- 공개 도구 4개는 노드 **종류·연결·설정**만 보고 `toolCode` 안의 코드 논리는 읽지 않는다.
  n8n audit의 위험 노드 목록에는 `toolCode`가 없다. Agentic Radar는 `toolCode`를 모두
  `code_interpreter`로 분류해 같은 "Arbitrary Code Execution" 경고를 붙인다.
  상세 수치는 `evidence/public-tools.json`.
- 팀 규칙은 7개 버전 모두 기대와 일치했다(`evidence/semgrep-summary.json`).

| 버전 | 검사 노드 | 미지원 | 경고(노드, jsCode 안의 줄) |
|---|---|---|---|
| before-feature | 9 | 7 | 없음 (도구 부재) |
| feature | 11 | 7 | Memory Delete 7, Memory Update 43 |
| before-fix | 12 | 8 | Memory Delete 7, Memory Update 51 |
| first-fix | 12 | 8 | 없음 |
| reverted | 12 | 8 | Memory Delete 7, Memory Update 51 |
| before-september | 12 | 8 | 없음 |
| september | 12 | 8 | 없음 |

미지원은 agent에 연결된 `toolWorkflow`(하위 workflow 호출)와 `telegramTool`이다. 조용히 건너뛰지
않고 결과에 `TARGET_TYPE_NOT_SUPPORTED`로 남긴다. "기능 추가 전 미탐지"는 전용 도구가 없어서이지,
기억을 바꿀 경로가 없다는 뜻이 아니다. 같은 버전의 범용 HTTP Tool은 모델이 method를 고르며
이 규칙의 범위 밖이다.

## 규칙의 동작과 한계

`runtime/static-scan/rules/agent-tool-write-without-ownership-check.yaml`
(`flowdelta.runtime.static_rules`로 생성, 손으로 고치지 않음)

1. source: 모델이 쓴 도구 입력 `query`.
2. sink: `helpers.httpRequest`의 method가 `DELETE`/`PATCH`/`PUT`일 때의 `url`.
3. 제외: sink보다 앞(최상위 또는 일반 블록 안)에 **비소유자를 막는 지원 guard**가 있음.
   - 소유자 필드(`owner_user_id` 등 9개)의 불일치(`!==`·`!=`)가 참일 때 `return`·`throw`하는 분기
   - 일치 비교를 담은 변수가 거짓일 때(`!own`) `return`·`throw`하는 분기 — n8n-claw 보완 코드의 형태
   - 쓰기가 일치(`===`·`==`) 분기 안에만 있음

**비교 구문 존재 ≠ 권한 강제 확인.** 0.4.0부터 비교식이 있다는 것만으로는 제외하지 않는다. 비교를
계산만 하거나, 불일치 때 로그만 남기거나, 소유자를 오히려 돌려보내는 코드는 경고한다. 따라서 경고는
"지원 guard로 비소유자 차단을 확인하지 못했다"는 뜻이고, "비교 코드가 없다"는 뜻이 아니다. 경고가 없는
것도 "정적으로 지원 guard를 찾았다"까지이며 실제 차단이나 정책 준수를 보인 것은 아니다.

사례 이름·노드 이름·테이블 이름은 규칙에 없다. 개발 중 세 번 고쳤다.
- 0.1.0: 아무 필드 비교나 검사로 오인해 Update 누락
- 0.2.0: 블록 안의 검사를 못 봐 보완된 Update 오탐
- 0.3.0 → 0.4.0: 비교식만 있어도 제외하던 미탐(PR #69 리뷰). 지원 차단 형태로만 제외하도록 좁혔다.

이 7개 버전에 맞춰 고쳤으므로 위 표는 **개발셋 결과**이며 일반화 성능 주장에 쓰지 않는다.
0.4.0에서도 7개 버전 결과는 0.3.0과 같다(노드·줄 동일, `evidence/semgrep-summary.json`).

팀이 직접 쓴 합성 코드(`tests/runtime/static_cases/`)로 고정한 동작과 알려진 한계:

| 경우 | 결과 |
|---|---|
| 검사 없이 DELETE (`vulnerable.js`) | 경고 |
| 비교값만 계산하고 DELETE (`unenforced_comparison_only.js`) | 경고 |
| 불일치 때 `console.log`만 하고 DELETE (`unenforced_mismatch_logs.js`) | 경고 |
| 소유자를 돌려보내고 비소유자는 DELETE (`unenforced_wrong_branch.js`) | 경고 |
| 불일치 때 `return` / `throw`, `!own`이면 `return`, 일치 분기 안에서만 쓰기 (`guarded*.js`) | 경고 없음 |
| 소유자 검사를 별도 함수로 분리 (`known_false_positive_helper.js`) | 오탐(판단 불가를 경고로 남김) |
| method를 변수에 담음 (`known_false_negative_method_variable.js`) | 미탐 |
| 비교식 피연산자 순서 반대(`caller !== row.owner_user_id`) (`known_false_positive_reversed_operands.js`) | 오탐(지원 형태가 아님) |
| 조건을 다시 부정(`if (!(owner !== caller)) return`) (`known_false_negative_negated_condition.js`) | 미탐(조건 안의 비교만 보고 극성은 따지지 않음) |
| `POST` 기반 쓰기, 모델이 method를 고르는 범용 HTTP Tool | 범위 밖 |
| 다른 노드·하위 workflow·DB 정책(RLS)의 검사 | 보지 못함 |

## 재현

Python 3.13, `uv`, Semgrep 1.181.0(`uv tool install semgrep==1.181.0`)이 필요하다. 저장소 루트에서:

```bash
uv run --frozen python experiments/n8n-claw-c/collect.py --out var/claw-c/sources
uv run --frozen python experiments/n8n-claw-c/extract.py --sources var/claw-c/sources --out var/claw-c/extracted
uv run --frozen python experiments/n8n-claw-c/scan.py --extracted var/claw-c/extracted --out var/claw-c/scan
```

- `collect.py`: 고정 URL에서 받고 SHA-256·크기를 확인한다. `var/` 밖·symlink 경로는 거부하고,
  이미 있는 파일은 덮어쓰지 않고 검증만 한다. 임시 파일에 쓴 뒤 이름을 바꾼다.
- `extract.py`: agent에 연결된 `toolCode`의 jsCode를 함수 하나로 감싸 저장하고, 원본 해시·
  JSON Pointer·코드 해시를 `index.json`에 남긴다(감싼 줄 1개만큼 줄 번호를 보정).
- `scan.py`: 규칙 파일이 생성본과 같고 Semgrep이 고정 버전인지 확인한 뒤 실행한다.
  원시 결과는 `var/`에만, 요약에는 규칙·노드·위치·해시만 남긴다.

주의: git 저장소 안에서 Semgrep은 **추적되는 파일만** 읽고 `tests/` 등을 건너뛴다. `var/`의
추출 파일은 추적되지 않아 그대로 실행하면 "0 files, 0 findings, 성공"이 나온다. 그래서
`--no-git-ignore`를 쓰고, 실제로 읽은 파일 목록이 추출한 파일과 다르면 그 버전을 `FAILED`로 남긴다.
Semgrep 오류도 `FAILED`다. 빈 결과를 "경고 없음"으로 바꾸지 않는다.

공개 도구 4개의 실행 명령과 조건은 `evidence/public-tools.json`의 `tools`에 적었다. n8n audit은
원본에 workflow `id`가 없어 `id`·`name`만 붙인 사본을 버전마다 새 n8n에 import했고, 활성화하지
않아 인증 없는 webhook 검사는 해당되지 않았다.

## 인계

- B(#50): 등록 template과 plan 규칙은 `docs/STATIC_SCAN.md`, 예시는 `examples/static-scan/`.
- 입력(#48): 공유 계약에 정적 검사를 넣는 방식은 #51 설계 검토 3-1의 합의가 필요하다.
- 실제 agent 행동 관측(동적)은 이 PR 범위가 아니다.
