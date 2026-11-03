# n8n-claw 소유권 누락 — C 정적 검사 도구 적합성

Refs #51. 대상 사례는 #48에서 선정하고 PR #57에 공유한 n8n-claw Memory Delete/Update의
소유권 확인 누락이다. 이 폴더는 정적 검사 도구의 개발셋 실행 기록과 재현 절차다.

**현재 규칙은 0.6.0이다.** 인증된 요청자·역할의 출처를 입력 계약으로 증명할 수 없어,
노드 출력이나 `admin` 변수 이름을 근거로 경고를 없애던 0.5.0의 억제를 제거했다.
0.6.0의 경고는 모델 입력이 고정 쓰기 요청의 URL에 도달하므로 권한 확인을 검토해야 한다는 뜻이다.
정상적인 권한 검사가 있어도 경고할 수 있으며, 첫 보완 전후의 안전성을 구분하는 규칙으로 쓰지 않는다.

정적 탐지는 "정적 조건 성립"까지만 말한다. agent가 도구를 고르는지, 타인의 기록이 실제로
바뀌는지는 확인하지 않는다. PR #57의 도구 JS 부분 실행과도 별개이며 서로 대신하지 않는다.

## 대상과 비교쌍

원본은 `sources.json`의 고정 commit 7개다(PR #57 `888de5d`의 sources.json과 같은 commit·해시).
원본 저장소에 LICENSE가 없어 원본·추출 코드·원시 Semgrep 결과는 `var/`에만 둔다.

| 비교 | 쌍 | case_id(#64와 같음) | 0.5.0 개발 당시 기대 |
|---|---|---|---|
| 기능 추가 | `9ebf36b` → `63a7f2e` | `n8n-claw-memory-feature-add` | 추가 후 탐지 |
| 첫 보완 | `4d1b1b7` → `eb6d7bc` | `n8n-claw-memory-first-fix` | 보완 전 탐지, 보완 후 미탐지 |
| 되돌림 | `eb6d7bc` → `2c6df44` | — | 되돌린 뒤 다시 탐지 |
| 후속 보완 | `c71b39e` → `fe57d16` | — | 둘 다 미탐지 |

취약 지점: AI Agent(`agent@3.1`)에 `ai_tool`로 연결된 `toolCode@1.2`의 **Memory Delete와
Memory Update**. 모델이 넘긴 `id`로 서비스 키를 써서 `DELETE`/`PATCH`를 보내며, 보완 전에는
대상 행의 `owner_user_id`와 요청자를 비교하지 않는다. 원저자의 첫 보완도 두 도구에 함께 검사를 넣었다.

## 현재 결과 — 규칙 0.6.0

2026-11-04 KST에 고정 원본 7개를 해시 검증한 뒤 Semgrep 1.181.0으로 다시 정적 검사했다.
네트워크를 끈 컨테이너에서 추출 JS 80개를 모두 읽었으며, 대상 workflow·JS는 실행하지 않았다.
7개 버전 모두 `SUCCEEDED`, Semgrep 오류 0개다. 결과는
[`evidence/semgrep-summary-0.6.0.json`](evidence/semgrep-summary-0.6.0.json)에 고정했고,
`evidence/semgrep-summary.json`은 같은 바이트의 현재 버전 별칭이다.

| 버전 | 검사 노드 | 미지원 | 경고(노드, jsCode 안의 줄) |
|---|---|---|---|
| before-feature | 9 | 7 | 없음 (전용 쓰기 도구 부재) |
| feature | 11 | 7 | Memory Delete 7, Memory Update 43 |
| before-fix | 12 | 8 | Memory Delete 7, Memory Update 51 |
| first-fix | 12 | 8 | Memory Delete 24·31·32, Memory Update 67 |
| reverted | 12 | 8 | Memory Delete 7, Memory Update 51 |
| before-september | 12 | 8 | Memory Delete 24·31·32, Memory Update 67 |
| september | 12 | 8 | Memory Delete 58·65·66, Memory Update 100 |

첫 보완 후에는 같은 Delete 노드의 여러 쓰기 요청이 각각 경고다. 경고 수를 취약점 수로 세지 않는다.
`basis=STATIC_CONDITION`, `security_outcome=null`이며 실제 침해·차단·정책 준수를 판정하지 않는다.
`examples/static-scan/runs/`의 finding·원시 보고서 해시·크기는 같은 규칙으로 두 버전을 별도 검사한
실제 출력에서 갱신했다. 이 파일의 run/plan 식별자는 여전히 수동 계약 예시다.

## 과거 도구 비교 — 규칙 0.5.0

당시에는 Memory Delete/Update의 보완 전후 경고 차이를 적합성 기준으로 삼았다.
아래는 과거 결과이며, 신뢰 근거 없는 억제가 발견되었으므로 현재 탐지 성능의 근거로 사용하지 않는다.

| 도구 | 공신력 | 기능 추가 후 새 도구 표시 | 소유권 누락 탐지 | 취약(`4d1b1b7`) vs 보완(`eb6d7bc`) |
|---|---|---|---|---|
| n8n audit (n8n 2.42.6) | n8n 공식 | 없음 | 없음 | 결과 동일 |
| Agentic Radar 0.14.1 | SplxAI(Zscaler) | 표시(ACE) | 없음 | 결과 동일 |
| FlowSentry 0.1.0 | 개인 | 없음 | 없음 | 결과 동일 |
| n8n-flowguard 0.1.1 | 개인 | 없음 | 없음 | 결과 동일 |
| **Semgrep 1.181.0 + 팀 규칙 0.5.0** | Semgrep 엔진 | 표시 | **Delete·Update** | **구분됨** |

- 공개 도구 4개는 노드 **종류·연결·설정**만 보고 `toolCode` 안의 코드 논리는 읽지 않는다.
  n8n audit의 위험 노드 목록에는 `toolCode`가 없다. Agentic Radar는 `toolCode`를 모두
  `code_interpreter`로 분류해 같은 "Arbitrary Code Execution" 경고를 붙인다.
  상세 수치는 `evidence/public-tools.json`.
- 0.5.0은 당시 7개 버전의 기대와 일치했다
  ([`evidence/semgrep-summary-0.5.0.json`](evidence/semgrep-summary-0.5.0.json)).
  0.4.0의 원본 기록도 [`evidence/semgrep-summary-0.4.0.json`](evidence/semgrep-summary-0.4.0.json)에 보존한다.

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
3. 소유자 비교·노드 출력·변수 이름을 이용한 억제는 없다. 경고 수준은 `WARNING`이다.

**비교 구문 존재 ≠ 권한 강제 확인.** Webhook 출력도 공격자가 쓸 수 있고, `|| input.caller` 같은
fallback이나 `const admin = input.x`도 신뢰된 주체·역할이 아니다. 현재 계약에는 인증 출처를 보장하는
정보가 없으므로 이들을 구문 모양만으로 구별하지 않는다. 경고 없음도 안전 판정이 아니다.
규칙 ID는 기록 연결을 위해 유지하지만, 현재 의미는 "소유권 검토가 필요한 쓰기 경로"다.

사례 이름·노드 이름·테이블 이름은 규칙에 없다. 변경 이력:
- 0.1.0: 아무 필드 비교나 검사로 오인해 Update 누락
- 0.2.0: 블록 안의 검사를 못 봐 보완된 Update 오탐
- 0.3.0 → 0.4.0: 비교식만 있어도 제외하던 미탐(PR #69 리뷰). 지원 차단 형태로만 제외하도록 좁혔다.
- 0.4.0 → 0.5.0: 복합 조건 속 비교와 모델 입력 비교도 guard로 인정하던 미탐(PR #83 리뷰). 요청자·대상 행의
  출처와 조건 전체의 형태를 함께 보도록 Semgrep 규칙 문법(`match`/`taint`의 `not`·`where`)을 바꿨다.
- 0.5.0 → 0.6.0: 노드 출력·fallback·관리자 변수 이름을 신뢰하던 미탐(PR #83 리뷰).
  인증 출처 계약 없이 안전성을 증명할 수 없어 억제를 제거했다. 첫 보완 버전도 경고한다.

이 7개 버전에 맞춰 고쳤으므로 모든 표는 **개발셋 결과**이며 일반화 성능 주장에 쓰지 않는다.
0.5.0은 0.3.0·0.4.0과 노드·줄이 같았지만 0.6.0에서는 경고의 의미와 결과가 달라졌다.

팀이 직접 쓴 합성 코드(`tests/runtime/static_cases/`)로 고정한 동작과 알려진 한계:

| 경우 | 결과 |
|---|---|
| 검사 없이 DELETE (`vulnerable.js`) | 경고 |
| 비교값만 계산하고 DELETE (`unenforced_comparison_only.js`) | 경고 |
| 불일치 때 `console.log`만 하고 DELETE (`unenforced_mismatch_logs.js`) | 경고 |
| 소유자를 돌려보내고 비소유자는 DELETE (`unenforced_wrong_branch.js`) | 경고 |
| 조건을 다시 부정(`if (!(owner !== caller)) return`) (`unenforced_negated_condition.js`) | 경고 |
| `불일치 && 모델입력`이면 return (`unenforced_and_condition.js`) | 경고 |
| `!own && !모델입력`이면 return (`unenforced_non_admin_conjunct.js`) | 경고 |
| 소유자를 모델 입력과 비교 (`unenforced_model_requester.js`, `_alias.js`) | 경고 |
| method를 변수에 담아 DELETE (`unenforced_method_variable.js`) | 경고 |
| `!own && !input.adminOverride`처럼 모델 입력 속성을 관리자 예외로 씀 (`unenforced_admin_from_input.js`) | 경고 |
| 불일치 때 `return` / `throw`, `!own`이면 `return`, `불일치 || X`, 관리자 예외, 일치 분기 안에서만 쓰기 (`guarded*.js`) | 경고(인증 출처 판단 불가) |
| 소유자 검사를 별도 함수로 분리 (`known_false_positive_helper.js`) | 오탐(판단 불가를 경고로 남김) |
| 비교식 피연산자 순서 반대(`caller !== row.owner_user_id`) (`known_false_positive_reversed_operands.js`) | 오탐(지원 형태가 아님) |
| 요청자를 `$json`·함수 인자 등 다른 경로로 받음, 조건 안에서 `$('노드')`를 바로 씀 | 오탐(지원 형태가 아님) |
| 위조 가능한 Webhook 요청자, 모델 입력 fallback, 모델 입력을 담은 `admin` 변수 (`untrusted_*.js`) | 경고 |
| `ctx.isAdmin`처럼 속성으로 된 실제 관리자 확인 | 오탐(속성 접근은 인정하지 않음) |
| 요청자 값 자체가 위조 가능한지(`qualifiedUserId`의 인증 경계) | 보지 못함 |
| `POST` 기반 쓰기, 모델이 method를 고르는 범용 HTTP Tool | 범위 밖 |
| 다른 노드·하위 workflow·DB 정책(RLS)의 검사 | 보지 못함 |
| GET 요청, URL이 고정이고 모델 입력은 body에만 있음, 로컬 결과만 반환 (`non_*.js`) | 경고 없음 |

현재 합성 코드 25개 중 쓰기 경로 22개가 경고하고 위 음성 대조군 3개는 경고하지 않는다.
합법적인 guard가 있는 표본의 경고는 알려진 오탐이며, 인증 출처 계약 없이 이를 줄였다고 주장하지 않는다.

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
