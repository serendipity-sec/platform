# 모듈 산출물 묶기와 주문 조회 통합 재현

공통 계약 0.4.0의 `write_module_bundle(handoff_dir, artifact_sources, output)`은
입력 handoff와 생산자가 명시한 JSON만 새 디렉터리에 담는다. 분석·검색·실행을
대신하지 않으며, 작성 성공과 `check-pipeline`의 연결 검사를 구분한다.

## 교수님께 보여줄 통합 실행

저장소 루트에서 Docker를 켜고 Python 3.13.15·uv 0.9.30을 사용한다.
아래 n8n digest는 `runtime/order-lookup/runtime.lock.toml`과 같다.

```powershell
$env:PYTHONUTF8 = "1"
uv sync --frozen --extra baseline
docker pull docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a
docker build --tag flowdelta-order-fixture:0.3.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline flowdelta demo-order-lookup --out var/demo-001
uv run --frozen --extra baseline flowdelta check-pipeline var/demo-001/bundle
```

다시 실행할 때는 `var/demo-002`처럼 새 경로를 사용한다.
실행 순서는 입력 정규화 → A 실제 분석 → B 실제 계획 → C 전후 격리 실행 →
공통 묶음 작성 → 참조 검증 → 기존 정상 기대 응답·서비스 이벤트·상태 확인이다.
`summary.json`의 `all_normal_checks_passed`와 양쪽 checks를 확인한다.
초기 handoff와 중간 산출물도 함께 남겨 어느 단계에서 실패했는지 추적한다.

## 생산자별 파일 배치

| 생산자 | 파일 | 보존할 출처 |
|---|---|---|
| 입력 담당 | `input/manifest.json`과 manifest에 적힌 입력 파일 | 원본 bytes·ID·SHA-256 |
| A | `analysis.json` | MODULE_OUTPUT |
| B | `plan.json`, `retrieval.json`, `kb-snapshot.json` | MODULE_OUTPUT 및 카드 원래 출처 |
| 등록 기준 | `template-catalog.json` | 기존 CONTRACT_EXAMPLE 그대로 |
| C | `runs/<side>-<attempt>.run.json`과 각 record가 명시한 JSON | RUNTIME_OBSERVATION |
| 작성기 | `manifest.json` | 전체 묶음 루트 기준 참조 |

입력 파일과 생산자 파일을 재직렬화하지 않는다. 기존 authorship, ID, 상대 경로,
해시를 그대로 유지한다. 중첩 ArtifactRef가 요구하는 파일은 호출자가 전부 명시해야 한다.
계약 예시를 묶는 것도 가능하지만 실제 A/B 출력으로 인정되지는 않는다.
C run이 없는 A/B 묶음의 `runtime_record_coverage`는 `NOT_SUPPLIED`다.

## 개별 writer 사용

```powershell
uv run --frozen --extra baseline flowdelta snapshot examples/order-lookup --out var/input-001
uv run --frozen --extra baseline flowdelta analyze var/input-001 --out var/analysis-001.json
uv run --frozen --extra baseline flowdelta bundle var/input-001 --artifact analysis.json=var/analysis-001.json --artifact template-catalog.json=examples/contracts/order-lookup/template-catalog.json --out var/analysis-bundle-001
```

`--artifact bundle-relative-path=source-file`을 파일마다 반복한다. `input/`은
writer가 handoff manifest를 읽어 선택하므로 직접 지정하지 않는다. B CLI에는
위 analysis bundle을 넘기고, B 출력과 C run JSON을 같은 방법으로 추가한다.
프로그램에서는 `dict[str, Path]`를 전달하면 된다. C 자료는 각 run의
`artifacts.artifacts`에 명시된 파일과 run record만 선택한다.

## 오류와 완료 경계

- 기존 출력: `OUTPUT_ALREADY_EXISTS`; 덮어쓰지 않는다.
- 누락: `ARTIFACT_SOURCE_MISSING`; 제공하지 않은 참조 파일도 포함한다.
- 해시/크기 변조: `ARTIFACT_INTEGRITY_MISMATCH`.
- 경로 충돌: `ARTIFACT_PATH_COLLISION`; 대소문자·파일/디렉터리 충돌을 포함한다.
- 입력/manifest 경로 예약, 경로 이탈, symlink/junction, private 파일을 거부한다.
- 8 MiB/파일, 32 MiB/묶음, 64개 artifact의 기존 계약을 유지한다.
- 임시 디렉터리에 먼저 쓰고 새 목적지를 예약한 뒤 manifest를 마지막에 게시한다.
  도중 실패한 출력에는 최상위 manifest가 없어 소비할 수 없다. 그 경로를 덮어쓰지 않는다.

공유 가능한 결과는 `bundle/`과 `summary.json`이다. `execution/private/`에는
DB·자격·상세 로그가 남으므로 공유하지 않는다. 보안 `outcome`은 계속 null이다.
정상 사례 한 건의 연결·재현 성공을 보안 취약점 탐지 성능이나 전체 제품 완성으로
확대 해석하지 않는다.
