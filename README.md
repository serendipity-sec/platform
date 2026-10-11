# FlowDelta · platform

n8n 변경 전후를 같은 업무·정책·환경·초기 상태에 놓고 분석할 수 있도록 만드는 프로젝트입니다.
현재 입력·정규화·제한된 노드 의미·공통 파일 계약과 두 정상 업무의 기준 환경을 구현했습니다.
A의 분석기, B의 검색·계획 생성기, C의 일반 실행 엔진은 팀원이 구현합니다.

**현재 계약: 0.4.0** · [입력 계약과 이전 방법](docs/INPUT_CONTRACT_04.md)

설계·개발 계획·준비 현황은 [팀 문서 저장소](https://github.com/serendipity-sec/docs)에서 관리합니다.
이 저장소는 코드·Schema·예제·실행 환경과 해당 버전의 사용 설명을 함께 제공합니다.

## 첫 실행

Python **3.13.15**, uv **0.9.30**, `uv.lock`을 사용합니다. 저장소 루트에서 실행합니다.
입력 처리에는 Docker·DB·모델 API 키가 필요하지 않습니다.

```powershell
uv sync --frozen --extra baseline
uv run --frozen --extra baseline flowdelta inspect-case examples/order-lookup
uv run --frozen --extra baseline flowdelta inspect-case examples/support-triage
uv run --frozen --extra baseline flowdelta check-examples examples/contracts/order-lookup
uv run --frozen --extra baseline flowdelta check-examples examples/contracts/support-triage
```

정규화 파일을 만들 때는 `var` 폴더를 먼저 준비하고 새 출력 경로를 사용합니다.

```powershell
uv run --frozen --extra baseline flowdelta snapshot examples/support-triage --out var/support-001
```

`inspect-case`와 `snapshot`은 원본 코드·표현식·HTTP 요청을 실행하지 않습니다.
출력의 `execution_status=NOT_RUN`은 정상입니다. 알려지지 않은 의미는 명시적으로 남습니다.

## 실제 n8n 정상 실행

Docker가 실행 중이어야 합니다. n8n **2.42.6** 이미지를 SHA-256 digest로 고정합니다.

```powershell
docker build --tag flowdelta-order-fixture:0.2.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline python -m flowdelta.cli baseline --out var/order-run-001
uv run --frozen --extra baseline python -m flowdelta.cli conformance --out var/nodes-run-001
```

- `baseline`: 전후 주문 조회, 독립 서비스 이벤트·상태·응답, n8n 노드 출력 비교.
- `conformance`: 문의 분류 전후와 분기·빈 배치·명시적 오류 처리·이름 참조의 등록된 8개 사례.
- 실행마다 별도 로컬 상태를 사용하며 끝나면 해당 컨테이너·네트워크를 정리합니다.
- `var/*/private/`에는 실행별 DB·자격이 남습니다. 공유 자료는 선택한 JSON 증거로 한정합니다.
- 두 명령은 팀 작성 고정 정상 사례용입니다. C가 만들 plan 기반 실행 엔진은 후속 구현입니다.

## 모듈 개발

- [팀 구현 계획과 현재 착수 명세](https://github.com/serendipity-sec/docs/blob/main/plans/team-implementation.md#current-start)
- [준비된 코드·실행 근거·남은 한계](https://github.com/serendipity-sec/docs/blob/main/plans/current-readiness.md)
- 함수 계약: [interfaces.py](src/flowdelta/interfaces.py), 데이터 계약: [입력 계약](docs/INPUT_CONTRACT_04.md)

`examples/contracts/`의 분석·계획은 **CONTRACT_EXAMPLE**입니다. 실제 모듈 출력이 아닙니다.
실제 출력은 `flowdelta check-pipeline <bundle>`로 파일 연결을 확인합니다.

## 개발 확인

```powershell
uv run --frozen --extra baseline ruff check src tests
uv run --frozen --extra baseline ruff format --check src tests
uv run --frozen --extra baseline mypy
uv run --frozen --extra baseline pytest
uv build
```

CI는 Windows/Ubuntu의 정적 검사·계약 검사를 수행하고 Linux에서 두 정상 실행 모음을 실행합니다.
기존 `experiments/`는 자동 실행하지 않습니다.

## 지원 범위

- [지원 표](schemas/support-matrix.json): 엔진·노드 버전·설정 조합별 10종의 제한된 어댑터.
- [입력 Schema](schemas/input-0.4.0.schema.json), [모듈 Schema](schemas/pipeline-0.4.0.schema.json).
- 비교마다 업무·정책·환경·초기 상태는 고정합니다. 변경 축은 workflow와 명시한 의존성입니다.
- 파일 8 MiB, 묶음 32 MiB, 64파일, JSON 깊이 64, 1,000노드, 10,000연결 상한을 둡니다.
- n8n 미지 필드는 원본과 opaque_fields에 보존합니다. 모르는 FlowDelta 계약 필드는 거부합니다.
- 의미 해시의 범위는 **선언된 설정**입니다. 전체 행동의 동등성·완전한 의존성 탐색을 증명하지 않습니다.
- 코드·AI 하위 노드·동적 하위 workflow·binary·반복/재시도·Merge의 다른 모드는 미지원입니다.
- 업무 성공, 자료 수집, 정리 상태를 분리합니다. 현재 보안 판정 outcome은 null입니다.

## 개발 규칙과 참고

- [기여 방법](CONTRIBUTING.md), [외부 구성요소](THIRD_PARTY.md)
- [전체 설계](https://github.com/serendipity-sec/docs/blob/main/final/README.md)
- [이전 준비·조사 기록](https://github.com/serendipity-sec/docs/blob/main/archive/README.md)

현재 예제는 팀 작성 합성 자료입니다. 운영 환경의 대표성을 입증한 최종 평가 데이터셋은 아닙니다.
프로젝트의 공개 라이선스·패키지 배포는 별도 결정 항목으로 남아 있습니다.
