# n8n-claw — 가장 가까운 외부 비교군의 동적 검사 적용 범위

Refs docs #52 (#67 1-3). 기존 조사([`final/research-evidence.md`](https://github.com/serendipity-sec/docs/blob/main/final/research-evidence.md))가
가장 가까운 외부 비교군으로 꼽은 [0xCD4/n8n-ai-agent-security-lab](https://github.com/0xCD4/n8n-ai-agent-security-lab)의
동적 검사(release rehearsal)가 우리 파일럿 n8n-claw Memory Delete에 쓰일 수 있는지 확인한다.

두 가지를 했다.

1. **코드 대조(`classify.py`)**: 고정 commit의 지원 범위 판정 코드를 읽고 그 순서를 옮겨 7개 버전에 적용했다.
2. **도구 실제 실행(`run_lab.py`)**: 같은 commit의 도구를 네트워크를 끈 컨테이너에서 실행했다.
   정적 검사(`bin/audit.mjs`)는 7개 버전, 동적 검사(`bin/rehearse.mjs`)는 두 쌍에 돌렸다.

## 도구 실제 실행 — [`evidence/tool-runs.json`](evidence/tool-runs.json)

```bash
python experiments/n8n-claw-c/collect.py --out var/claw-c
python experiments/n8n-claw-lab/run_lab.py --out experiments/n8n-claw-lab/evidence/tool-runs.json
```

- 도구를 `var/n8n-claw-lab/tool`에 고정 commit으로 받고, `node`(digest 고정) 컨테이너에서
  `--network none`·읽기 전용으로 실행한다. 도구 코드는 호스트에서 돌지 않는다.
- 원시 출력은 upstream 노드 이름·경로를 담아 `var/n8n-claw-lab/raw`에만 둔다. 커밋한 요약에는
  종료 코드·점수·finding ID와 심각도·전후 동일 여부·rehearsal 결과와 사유 코드만 있다.
- rehearsal fixture는 도구의 예제 fixture를 그대로 썼다. 지원 범위 검사에서 먼저 멈추므로 결과에 영향이 없다.

**정적 검사 (2026-11-11)**

| 버전 | 점수 | finding | 기억 도구 언급 |
|---|---|---|---|
| before-feature · feature | 46 (D) | 5개 (high 3, medium 1, low 1) | 없음 |
| before-fix · first-fix · reverted · before-september · september | 24 (F) | 7개 (high 4, medium 2, low 1) | 없음 |

- `before-feature → feature`(기억 삭제·수정 도구 추가)와 `before-fix → first-fix`(소유권 검사 추가)는
  finding이 **완전히 같다**. 이 도구의 정적 검사도 `n8n-claw-c`의 공개 도구 4개처럼 두 쌍을 구분하지 못한다.
- finding은 모두 workflow 전체 구조에 대한 것이다(신뢰되지 않은 입력 → 모델, 승인 없는 외부 쓰기,
  비용 제한 없음, 데이터로 만든 HTTP URL 등). `toolCode` 안의 소유자 비교는 보지 않는다.

**동적 검사 (rehearsal)**

| 쌍 | 종료 코드 | 결과 | 관측한 동작 |
|---|---|---|---|
| feature-add | 3 | `BLOCKED_UNSUPPORTED` | 0 (Docker 실행 안 함) |
| first-fix | 3 | `BLOCKED_UNSUPPORTED` | 0 (Docker 실행 안 함) |

실제 사유 코드에는 `classify.py`가 예측한 6개(`ACTIVE_NODE_LIMIT_EXCEEDED`, `CODE_NODE_BLOCKED`,
`CREDENTIAL_REFERENCE_PRESENT`, `METHOD_READ_ONLY_UNSUPPORTED`, `NODE_UNSUPPORTED`, `SUBWORKFLOW_UNSUPPORTED`)가
모두 있고, 옮기지 않은 경로·구조 검사의 사유(`PATH_*`, `ACTION_PARAMETERS_UNSUPPORTED`,
`STRUCTURAL_VERSION_UNSUPPORTED`, `TRANSFORMATION_FIDELITY_UNPROVEN`)가 더 있다.

## 코드 대조 (`classify.py`)

아래는 도구를 실행하기 전에 한 코드 대조 기록이다. 결론은 위 실제 실행과 같다.

## 대상

| 항목 | 값 |
|---|---|
| 도구 | `0xCD4/n8n-ai-agent-security-lab`, MIT |
| 고정 commit | `9d105b3bda7eeb7b382b16b0dc67e5e0c271c5d8` (2026-08-21, 최신 릴리스 태그 `v1.3.0`) |
| 옮긴 코드 | `src/release-rehearsal-preflight.mjs`의 `classifyReleaseRehearsalNode()`·`classifyHttpNode()`, 노드 수 상한 |
| 입력 | `experiments/n8n-claw-c/sources.json`의 고정 원본 7개 (해시 검증) |

## 재현

저장소 루트에서 실행한다. 원본은 LICENSE가 없어 `var/`에만 받는다.

```bash
python experiments/n8n-claw-c/collect.py --out var/claw-c
python experiments/n8n-claw-lab/classify.py --out experiments/n8n-claw-lab/evidence/rehearsal-scope.json
```

`classify.py`는 표준 라이브러리만 쓰고 네트워크를 쓰지 않는다. 원본 해시가 `sources.json`과 다르면 멈춘다.
결과 파일에는 노드 종류·사유 코드·개수와 두 기억 도구의 이름만 남기고, 코드·프롬프트·URL은 남기지 않는다.

## 결과 — [`evidence/rehearsal-scope.json`](evidence/rehearsal-scope.json)

| 버전 | 노드 (활성) | 활성 노드 상한 20 | 막히는 노드 | 진입 트리거 | Docker 실행 시작 |
|---|---:|---|---:|---|---|
| before-feature | 48 | 초과 | 45 | Webhook·Telegram·하위 workflow 3개 | 안 함 |
| feature | 50 | 초과 | 47 | 3개 | 안 함 |
| before-fix | 58 | 초과 | 53 | 3개 | 안 함 |
| first-fix | 58 | 초과 | 53 | 3개 | 안 함 |
| reverted | 58 | 초과 | 53 | 3개 | 안 함 |
| before-september | 60 | 초과 | 55 | 4개 (Webhook 2) | 안 함 |
| september | 61 | 초과 | 56 | 4개 (Webhook 2) | 안 함 |

막히는 사유(before-fix 기준):

| 사유 | 개수 | 노드 종류 |
|---|---:|---|
| `CREDENTIAL_REFERENCE_PRESENT` | 18 | Postgres 7, Telegram 5, OpenAI 2, Anthropic 채팅 모델·Telegram 도구·Telegram 트리거·Webhook 각 1 |
| `NODE_UNSUPPORTED` | 16 | AI 도구 코드(`toolCode`) 12, AI Agent·Merge·파일 추출·하위 workflow 트리거 각 1 |
| `CODE_NODE_BLOCKED` | 11 | Code |
| `SUBWORKFLOW_UNSUPPORTED` | 7 | 하위 workflow 호출 |
| `METHOD_READ_ONLY_UNSUPPORTED` | 1 | `GET` HTTP Request |

문서상 결과는 `BLOCKED_UNSUPPORTED`(종료 코드 3)이다.

**Memory Delete·Memory Update**는 두 버전 쌍 모두 `@n8n/n8n-nodes-langchain.toolCode` 1.2라서 `NODE_UNSUPPORTED`다.

## 읽을 때 주의할 것

- **이 도구의 동적 검사가 보는 효과와 우리 사례의 효과가 다르다.**
  - rehearsal은 HTTP Request 노드의 `POST`·`PUT`·`PATCH` 요청을 로컬 수신 서비스로 돌려, 전후 버전의 요청 차이를 본다.
  - `DELETE`는 정책으로 막는다(`METHOD_DELETE_BLOCKED`).
  - 우리 사례는 AI 도구 코드가 서비스 키로 남의 기억을 `DELETE`하는 것이다. 노드 종류가 지원되더라도 이 효과는 관측 대상이 아니다.
- **"Docker 실행 안 함"은 도구가 틀렸다는 뜻이 아니다.** 도구는 범위 밖 입력을 실행 전에 거부하도록 설계됐다.
  비교 실험에서는 이 사례를 이 도구의 "지원 안 함"으로 세고, 점수 비교에서 빼되 지원 표에는 남긴다.
- 옮긴 판정은 HTTP 목적지 검사와 연결 그래프 검사(순환·분리 노드·`main[0]` 경로)를 포함하지 않는다.
  이 사례는 그 전 단계에서 이미 막히므로 결론은 바뀌지 않는다.
