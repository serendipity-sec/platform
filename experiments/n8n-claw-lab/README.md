# n8n-claw — 가장 가까운 외부 비교군의 동적 검사 적용 범위

Refs docs #52 (#67 1-3). 기존 조사([`final/research-evidence.md`](https://github.com/serendipity-sec/docs/blob/main/final/research-evidence.md))가
가장 가까운 외부 비교군으로 꼽은 [0xCD4/n8n-ai-agent-security-lab](https://github.com/0xCD4/n8n-ai-agent-security-lab)의
동적 검사(release rehearsal)가 우리 파일럿 n8n-claw Memory Delete에 쓰일 수 있는지 확인한다.

**도구를 실행한 결과가 아니다.** 고정 commit의 지원 범위 판정 코드를 읽고, 그 판정 순서를 `classify.py`로 옮겨
n8n-claw 7개 버전에 적용했다. 실제 도구는 아래 표의 사유보다 먼저 다른 이유로 멈출 수 있다.
도구 자체 실행은 따로 승인을 받은 뒤 같은 입력으로 재현한다.

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
- **정적 검사(`bin/audit.mjs`)는 아직 돌리지 않았다.** 같은 7개 버전에 돌리면
  [`n8n-claw-c`](../n8n-claw-c/README.md)의 공개 정적 도구 4개(n8n audit, Agentic Radar, FlowSentry, n8n-flowguard) 표에 한 줄을 더할 수 있다.
- 옮긴 판정은 HTTP 목적지 검사와 연결 그래프 검사(순환·분리 노드·`main[0]` 경로)를 포함하지 않는다.
  이 사례는 그 전 단계에서 이미 막히므로 결론은 바뀌지 않는다.
