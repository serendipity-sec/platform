# FlowDelta · platform

n8n의 변경 전후 workflow를 동일한 업무·정책·환경·초기 상태와 함께 다루는 개발 저장소입니다.
현재 구현은 **L-START: 로컬 입력 묶음 → 구조 정규화 → A/C 인계 파일**까지입니다.

## 처음 실행하기

Python 3.13.15와 uv 0.9.30을 기준으로 합니다. 의존성은 `uv.lock`에 고정합니다.
저장소 루트에서 실행하세요. n8n·Docker·DB·모델 API는 이 첫 명령에 필요하지 않습니다.

```powershell
uv sync --frozen
uv run --frozen flowdelta --help
uv run --frozen flowdelta inspect-case examples/order-lookup
mkdir var
uv run --frozen flowdelta snapshot examples/order-lookup --out var/order-lookup-001
```

`var`가 이미 있다면 `mkdir`은 생략합니다. 다시 출력할 때는 `--out`에 새 폴더명을 사용합니다.
기존 출력과 입력 폴더를 덮어쓰지 않습니다. 부모 폴더는 먼저 만들어 두세요.

첫 출력에서 확인할 값은 다음과 같습니다.

```json
{
  "schema_version": "0.3.0",
  "processing_status": "STRUCTURE_PARSED",
  "semantic_support": "UNKNOWN",
  "execution_status": "NOT_RUN"
}
```

정상 예제는 전후 각각 노드 3개와 연결 2개입니다. 모든 노드 의미는 아직 `UNKNOWN`입니다.
원본의 표현식·HTTP 요청·코드를 실행하지 않습니다.

## 어디서 시작하나요?

| 역할 | 지금 사용할 자료 | 다음 구현 |
|---|---|---|
| 팀장 | `src/flowdelta/`, `examples/order-lookup/` | L-03의 첫 의미 어댑터와 C의 runtime 조합 연결 |
| A | `before.normalized.json`, `after.normalized.json`, `semantics.json` | 구조 그래프·노드 대응·변경 목록 |
| B | `case.json`, `task.json`, `policy.json`와 상세 작업 카드 | 카드 계약·파일 검색·고정 정상 계획 |
| C | `case.json`, `snapshots.json`, `manifest.json`과 원본 파일 | 로컬 합성 서비스·runtime 고정·정상 업무 실행 |

**[팀장 착수·인계 문서](docs/LEADER_START.md)**에 전달 순서, 첫 작업 카드,
완료 산출물, 아직 결정할 사항을 적었습니다. 특정 실명·마감을 배정하지 않았습니다.

## 개발 확인

```powershell
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen mypy
uv run --frozen pytest
uv build
```

테스트는 `tests/`만 실행합니다. 기존 `experiments/`는 자동 실행하지 않습니다.
CI는 Ubuntu와 Windows에서 동일한 입력 계약을 확인합니다.
JSON Schema는 [`schemas/input-0.3.0.schema.json`](schemas/input-0.3.0.schema.json)에 있습니다.
`uv run --frozen flowdelta schema`로 같은 내용을 출력할 수 있습니다.

## 현재 경계

- 입력 모델은 0.3.0의 첫 부분 구현입니다. 변경 축은 `workflow` 한 개만 허용합니다.
- 업무·정책·환경·초기 상태는 전후에 같은 파일을 참조합니다. 정책 문장은 아직 기계적으로 해석하지 않습니다.
- JSON 파일 1개 8 MiB, 묶음 32 MiB, 최대 64개 파일, JSON 깊이 64, 노드 1,000개, 연결 10,000개를 상한으로 둡니다.
- 알려지지 않은 n8n 필드는 `opaque_fields`와 원본에 보존합니다. 알 수 없는 FlowDelta 계약 필드는 거부합니다.
- 구조 해시는 공백·JSON 객체 키 순서만 정리합니다. 좌표·노드 이름·배열 순서는 보존합니다. 의미 해시는 `null`입니다.
- 하위 workflow·도구 의존성 해석, 실제 n8n 동작, 의미 diff, RAG, 실행 엔진은 후속 작업입니다.
- 예제는 팀 작성 합성 자료입니다. 운영 데이터의 대표성이나 실제 n8n 호환성을 확인한 데이터셋이 아닙니다.
- 출력에는 원본도 복사됩니다. 공유용 입력은 자격 값·개인정보를 제거하고 자격 참조만 사용해야 합니다.

## 문서와 이력

- [4인 구현 계획](https://github.com/serendipity-sec/docs/blob/main/plans/team-implementation.md)
- [기여 방법](CONTRIBUTING.md) · [외부 구성요소](THIRD_PARTY.md)
- [이전 플랫폼 설명의 Git 이력](https://github.com/serendipity-sec/platform/blob/8ee67c2032f21b82bb36127acaf9095e05b7f6a7/README.md)

공개 라이선스·패키지 배포 여부는 아직 결정하지 않았습니다. 공개 배포는 별도 단계입니다.
