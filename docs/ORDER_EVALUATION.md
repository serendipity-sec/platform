# 주문 소유권 계열의 A→B→C 통합

관련: #119, 선행 #114·#117, 변형의 출발점 #113 (`4b4dba5`).

한 명령으로 등록 비교쌍 입력 → A 변경·영향 분석 → B 시험 선택 → C 실제 n8n 전후 실행 →
독립 서비스 증거의 자동 판정을 연결한다. 제품 판정은 자동이며 사람 라벨링을 실행 조건으로
요구하지 않는다. **순수 합성 개발·통합 계열**이다. 독립 평가 정답이나 최종 평가셋의 탐지
정확도 측정으로 사용하지 않는다. 외부 모델 API, 유료 서비스, PostgreSQL은 필요하지 않다.

## 실행

저장소 루트에서 Python 3.13, uv, Docker를 사용한다. n8n은 기존 lock의 2.42.6 digest다.
이미지는 명시적으로 준비하며 런처가 자동 다운로드·빌드하지 않는다.

```powershell
uv sync --frozen --extra baseline --extra knowledge
docker build --tag flowdelta-order-fixture:0.4.0 --file runtime/order-lookup/fixture.Dockerfile .
uv run --frozen --extra baseline flowdelta evaluate-order-family --manifest runtime/order-evaluation/family.json --out var/order-prepare
uv run --frozen --extra baseline flowdelta evaluate-order-family --manifest runtime/order-evaluation/family.json --out var/order-execute --execute
uv run --frozen --extra baseline flowdelta check-pipeline var/order-execute/p0/s1/b
uv run --frozen --extra baseline flowdelta judge-order-evaluation-bundle var/order-execute/p0/s1/b --out var/order-recheck
```

기본은 PREPARED이며 Docker를 실행하지 않는다. `--execute`는 6쌍 × 3시나리오 × 전후 =
36회 실행한다. 반복 1회, 동시 실행 1개, 자동 재시도 없음이다. 각 실행은 별도 Compose
project·상태·자격·request ID를 사용한다. 출력은 새 `var/` 하위 경로만 허용한다. Windows는
짧은 출력 경로를 권장한다. 기존 출력을 덮어쓰거나 이어서 실행하지 않는다.

`COMPLETE`는 등록 시험의 실행·수집·판정·정리가 완료됐다는 뜻이다. 위반 발견도 성공적으로
완료된 시험이다. 실패·증거 부족·정리 실패는 INCOMPLETE와 종료 코드 2이며, 중단은 부분
요약·할당된 실행 경로·해당 실행 기록을 보존한 뒤 전파한다.

## 등록한 변형과 비교쌍

모든 변형에서 요청 주체는 fixture가 고정한 customer-demo / tenant-demo다. 요청 본문의
customer_id·tenant_id는 공격자가 바꿀 수 있는 입력이다.

| 변형 | 동작 |
|---|---|
| v0 | 기존 서비스가 소유권을 검사하는 주문 조회 |
| v1 | 소유권 확인 없이 내부 주문 내용 조회 |
| v2 | 신뢰된 context → metadata → customer 및 tenant 검사 → 내용 조회 |
| v3 | metadata의 소유자를 요청 본문의 customer 및 tenant와 비교 |
| v0-display | v0 응답에 표시 필드만 추가 |
| v2-display | v2 응답에 표시 필드만 추가 |

v2는 주문을 읽은 뒤 응답만 숨기는 방식이 아니다. customer와 tenant는 각각 하나의 엄격한
If 비교로 확인한다. 불일치와 HTTP 오류는 내용 읽기 전에 응답 분기로 종료한다. metadata
조회는 평가 정책에서 명시적으로 허용한다. #113의 `?owner=` API나 실행 결과를 그대로
이식한 것이 아니므로, 새 환경의 독립 증거로 다시 판정한다.

| 비교쌍 | 전 → 후 | 통합 검증 목적 |
|---|---|---|
| p01 | v0 → v1 | 새 위반 |
| p02 | v0 → v3 | 사칭 입력에서 새 위반 |
| p03 | v1 → v2 | 수정 효과 |
| p04 | v1 → v3 | 수정 후 남은 위반 |
| p05 | v0 → v0-display | 정상 변경 |
| p06 | v2 → v2-display | 수정 상태 유지 |

세 시험은 정상 주문(ORD-1001), 타인 주문(ORD-2001, 정직한 요청 본문), 소유자 사칭
(ORD-2001, other-demo / other-tenant 본문)이다. 기대 판정표는 테스트·검증용이며 제품의
A/B 입력이나 판정기 입력으로 전달하지 않는다. 순수 합성 계열 안의 반복 비교이며 독립된
6개 공개 계열로 집계하지 않는다.

## 계약과 결과 읽기

- 기존 `TestPlan.normal_input`은 정상 업무 입력을 유지한다. 보안 시험은 새 template mode
  `REGISTERED_SECURITY_REQUEST`와 `extensions["flowdelta.order_scenario"]`의 ArtifactRef로
  연결한다. 참조 문서는 `scenario_format=flowdelta-order-scenario-v1`, scenario_id,
  request_input을 가진다. C가 등록 원본 bytes·크기·해시·입력 형식을 확인한다.
- B는 등록된 own-order 정책과 주문 읽기 경로로 적용 여부를 판단한다. 일반 업무 카드의
  검색 결과와 A의 analysis ID·영향 경로 ID·우선순위 근거를 세 계획에 보존한다. 시험 종류와
  예산은 고정하며, 이번 계열만으로 시험 선택 방법의 우월성을 주장하지 않는다.
- 각 계획은 별도 공통 bundle을 사용한다. 0.4.0의 단일 plan 소비자와 파일 수 상한을 유지하고,
  계획 묶음의 연결은 `summary.json`의 pairs/trials 배열로 제공한다. 기존 정상 template에
  시나리오를 붙이면 거부한다.
- 정식 등록은 workflow만이 아니라 정확한 전후 쌍·업무·정책·환경·초기 상태·catalog·시나리오를
  고정한다. 동적 URL 의미는 평가 opt-in 아래 고정 orders-api 주소 + 주문 ID + 선택적인
  `/ownership` 형식에 한정한다. 임의 URL이나 코드를 실행하지 않는다.
- `summary.json`: 단계 결과, source/config 해시, A 분석·B 선택·계획·실행·판정 참조.
  `report.md`: 전후 판정과 정상 업무 결과 표. `p*/s*/judgment.json`: 판정 근거와 원본 해시.
  `p*/s*/b`: 독립적으로 `check-pipeline` 및 재판정 가능한 최종 bundle.
- 각 `e/private/`에는 자격·DB·진단 로그가 남는다. 공유하지 않는다. 공유용 bundle은 명시적인
  합성 JSON 산출물만 담는다. 기본 출력 경로 전체를 커밋하지 않는다.

독립 fixture 이벤트에서 `ORDER_READ + RETURNED`만 내용 읽기다. NOT_FOUND는 효과도 명시적
거절도 아니며, 완전한 완료 실행에서 NOT_OBSERVED_COMPLETE다. metadata만 읽고 내용 호출을
하지 않은 정상 거절 역시 관측 범위의 미관측이다. BLOCKED는 독립적인 명시적 DENIED 증거가
있을 때만 쓴다. 신원·request/run·상태·sequence·파일 해시가 맞지 않으면 INCONCLUSIVE다.
금지 효과 후 실행 오류가 나도 완전한 증거의 위반은 남는다. `security_outcome`은 기존처럼 null이고
보안 판정은 별도 보고서에서 읽는다. read-only 정책 전체를 검증하는 범용 판정기는 아니다.

## 유지보수와 검증

`tools/make_order_evaluation.py`는 팀 작성 자료를 결정적으로 생성한다. 기본은 생성 결과와
등록 해시 일치 검사이며, `--write`만 파일·등록 해시를 갱신한다. 등록 갱신은 변경 검토 후
명시적으로 수행한다. 이 명령은 실행 시 자동 호출되지 않는다. runtime 공통 소스 변경 시
#114 계약 점검용 manifest 해시도 함께 갱신한다.

```powershell
uv run --frozen --extra baseline python tools/make_order_evaluation.py
uv run --frozen --extra baseline pytest tests/runtime/test_order_evaluation.py
uv run --frozen --extra baseline --extra knowledge ruff check src tests tools/make_order_evaluation.py
uv run --frozen --extra baseline --extra knowledge mypy
uv run --frozen --extra baseline --extra knowledge pytest
```

Docker 대역 테스트는 요청 전달·격리·파일 연결·변조 거부·중단 정리를 확인한다. 실제 n8n의
36회 동작 및 기대 판정 일치는 별도의 실행 보고서에서 확인한다. 기존 정상 주문 데모,
일반 파이프라인, 저장된 정상 bundle 재판정도 회귀 검증한다.

민우 검토 항목은 변형 의미·출처·비교쌍 구성이다. 준서 검토 항목은 C 실행·요청 귀속·증거
완전성·판정이다. 코드 검토는 제품 판정 경로에 사람 판정 단계를 추가한다는 뜻이 아니다.
