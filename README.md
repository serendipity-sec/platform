# FlowDelta · platform

n8n의 변경 전후 workflow를 동일한 업무·정책·환경·초기 상태와 함께 다루는 개발 저장소입니다.
현재 **입력·정규화·첫 3종 의미 어댑터·공통 계약·주문 조회 정상 실행 환경**이 준비됐습니다.
A의 분석기, B의 계획 생성기, C의 일반 실행 엔진은 아래 예제로 각각 착수합니다.

**[팀장 기준 환경과 상세 설계](docs/LEADER_BASELINE.md)** ·
**[4인 구현 착수 카드](docs/TEAM_TASKS.md)**

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
  "semantic_support": "SUPPORTED",
  "execution_status": "NOT_RUN"
}
```

정상 예제는 전후 각각 노드 3개와 연결 2개입니다. 등록된 세 노드 조합의 의미를 제공합니다.
위의 inspect/snapshot 명령은 원본의 표현식·HTTP 요청·코드를 실행하지 않습니다.
profile 밖의 의미는 `UNKNOWN` 또는 `UNSUPPORTED`로 남깁니다.

## 실제 첫 정상 업무 실행

Docker가 실행 중인 저장소 루트에서 다음을 실행합니다.

```powershell
uv sync --frozen --extra baseline
uv run --frozen --extra baseline flowdelta check-examples examples/contracts/order-lookup
docker build --tag flowdelta-order-fixture:0.1.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline python -m flowdelta.cli baseline --out var/baseline-new
```

공식 n8n **2.42.6** 이미지는 digest로 고정됩니다. before/after를 별도 초기 상태에서 실행하고
응답·서비스 이벤트·주문 상태·정리 결과를 남깁니다. 로컬 자격과 DB는 `var/*/private/`에 보관합니다.
이 명령은 등록된 팀 작성 정상 주문 조회만 실행합니다. 상세 절차와 초기화는 기준 환경 문서를 읽으세요.

## 어디서 시작하나요?

| 역할 | 지금 사용할 자료 | 다음 구현 |
|---|---|---|
| 팀장 | `src/flowdelta/`, `examples/order-lookup/` | 입력·의미 어댑터 확장, 두 번째 업무 |
| A | `examples/contracts/order-lookup/input/`, 분석 기대 예시 | 실제 그래프·노드 대응·변경 목록 생성 |
| B | 분석 기대 예시·카드 2개·카탈로그 | 카드 로더·검색·정상 계획 자동 생성 |
| C | 계획 기대 예시·동작하는 baseline | 계획을 읽는 실행 엔진·상태 분리·초기화 |

작업 폴더·함수·입력·출력·오류·완료 조건은 **[4인 구현 착수 카드](docs/TEAM_TASKS.md)**에 있습니다.
특정 실명·마감은 아직 배정하지 않았습니다. [기존 입력 인계 문서](docs/LEADER_START.md)는 이력으로 보존합니다.

## 개발 확인

```powershell
uv sync --frozen --extra baseline
uv run --frozen --extra baseline ruff check src tests
uv run --frozen --extra baseline ruff format --check src tests
uv run --frozen --extra baseline mypy
uv run --frozen --extra baseline pytest
uv build
```

테스트는 `tests/`만 실행합니다. 기존 `experiments/`는 자동 실행하지 않습니다.
CI는 Ubuntu와 Windows에서 공통 계약을 확인하며 Linux에서 실제 정상 n8n 실행도 수행합니다.
JSON Schema는 [`schemas/input-0.3.0.schema.json`](schemas/input-0.3.0.schema.json)에 있습니다.
`uv run --frozen flowdelta schema`로 같은 내용을 출력할 수 있습니다.
파이프라인 Schema는 [`schemas/pipeline-0.3.0.schema.json`](schemas/pipeline-0.3.0.schema.json),
예시는 [`examples/contracts/order-lookup/`](examples/contracts/order-lookup)에 있습니다.
수작업 예시는 `CONTRACT_EXAMPLE`로 표시하며 실제 모듈 생성물과 구별합니다.

## 현재 경계

- 입력 모델은 0.3.0의 첫 부분 구현입니다. 변경 축은 `workflow` 한 개만 허용합니다.
- 업무·정책·환경·초기 상태는 전후에 같은 파일을 참조합니다. 정책 문장은 아직 기계적으로 해석하지 않습니다.
- JSON 파일 1개 8 MiB, 묶음 32 MiB, 최대 64개 파일, JSON 깊이 64, 노드 1,000개, 연결 10,000개를 상한으로 둡니다.
- 알려지지 않은 n8n 필드는 `opaque_fields`와 원본에 보존합니다. 알 수 없는 FlowDelta 계약 필드는 거부합니다.
- 구조 해시는 공백·JSON 객체 키 순서만 정리합니다. 좌표·노드 이름·배열 순서는 보존합니다. 의미 해시는 `null`입니다.
- 하위 workflow·도구 의존성 해석, 의미 diff, RAG, 일반 실행 엔진은 후속 작업입니다.
- 세 노드의 등록된 정상 사례는 실제 실행했습니다. 전체 n8n 동작의 호환성을 주장하지 않습니다.
- 예제는 팀 작성 합성 자료이며 운영 데이터의 대표성을 입증한 데이터셋이 아닙니다.
- 출력에는 원본도 복사됩니다. 공유용 입력은 자격 값·개인정보를 제거하고 자격 참조만 사용해야 합니다.

## 문서와 이력

- [4인 구현 계획](https://github.com/serendipity-sec/docs/blob/main/plans/team-implementation.md)
- [기여 방법](CONTRIBUTING.md) · [외부 구성요소](THIRD_PARTY.md)
- [이전 플랫폼 설명의 Git 이력](https://github.com/serendipity-sec/platform/blob/8ee67c2032f21b82bb36127acaf9095e05b7f6a7/README.md)

공개 라이선스·패키지 배포 여부는 아직 결정하지 않았습니다. 공개 배포는 별도 단계입니다.
