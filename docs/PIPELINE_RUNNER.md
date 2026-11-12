# 명시적인 입력을 받는 통합 runner

[#48](https://github.com/serendipity-sec/platform/issues/48)의 고정 profile·입력 repository 위에
기존 A 분석, B 검색/계획, C 등록 실행, 공통 bundle writer를 연결합니다.
사례별 경로와 기대 정답을 runner에 넣지 않고 호출자가 case/profile/catalog/cards를 지정합니다.

## 실행

[고정 profile](FIXED_PROFILE.md)을 먼저 검토·생성하고 출력 해시를 보관합니다.
다음 명령은 입력 저장/조회 → A → B → 파일 연결 검사까지 수행하며 C는 `NOT_RUN`입니다.

```powershell
uv run --frozen --extra baseline flowdelta run-pipeline examples/order-lookup --profile var/order-profile.json --profile-sha256 <보관한-해시> --catalog examples/contracts/order-lookup/template-catalog.json --cards examples/contracts/order-lookup/cards --out var/pipeline-prepare
```

Docker가 준비됐고 등록 템플릿을 실제 실행하려면 새 출력 경로와 `--execute`를 사용합니다.

```powershell
uv run --frozen --extra baseline flowdelta run-pipeline examples/order-lookup --profile var/order-profile.json --profile-sha256 <보관한-해시> --catalog examples/contracts/order-lookup/template-catalog.json --cards examples/contracts/order-lookup/cards --out var/pipeline-execute --execute
uv run --frozen --extra baseline flowdelta check-pipeline var/pipeline-execute/bundle
```

C의 기존 preflight·등록 template·원본 해시·환경 검사를 그대로 사용합니다. `--execute`는
임의 workflow를 허용하지 않습니다. 현재 등록 범위는 주문 조회이며 다른 사례는 명시적인 오류로
남습니다. 등록되지 않은 지원 문의 분류 사례는 A/B 준비가 성공해도 C 실행이 거부됩니다.

## 결과와 실패

- `input-record.json`, `inputs/`: 고정 profile 검사와 정확한 ID의 로컬 입력 repository.
- `producers/`: 실제 A의 analysis, B의 retrieval/plan/KB snapshot, 고정한 catalog 원문.
- `analysis-bundle/`, `plan-bundle/`: 각 소비자가 읽는 공통 계약. C는 작성한 plan을 다시 검증합니다.
- `execution/`: 명시적으로 실행한 경우에만 생성되는 C 기록. private 하위 폴더는 공유하지 않습니다.
- `bundle/`: 선택된 원본/산출물만 기존 writer로 묶고 실제 소비자 검사로 확인한 결과.
- `summary.json`: 호출 ID, package 버전과 전체 Python 구현·runtime 설정·dependency lock의 파일 해시,
  그 목록의 `source_tree_sha256`, profile/case/snapshot/analysis/
  retrieval/plan/run ID, catalog/KB/bundle 해시, 단계별 상태, 오류 코드.

준비 성공은 `PREPARED`와 실행 `NOT_RUN`입니다. C가 실행·수집·정리를 모두 완료하면
`EXECUTED`입니다. C의 실패/자료 부족/정리 실패는 `INCOMPLETE`와 exit 2이며, 입력/분석/계획/
파일 오류는 해당 단계를 `FAILED`로 기록하고 exit 2를 반환합니다. 진행하지 않은 단계는
`NOT_RUN`으로 남깁니다. 실행 중에는 `RUNNING`, 사용자 중단은 `INTERRUPTED`와 해당 단계로 남깁니다.
이미 할당된 실행 ID도 보존합니다. 요약 저장까지 실패하더라도 원래 오류를 덮어쓰지 않습니다.
기존 출력은 덮어쓰지 않습니다.

최종 bundle은 C가 읽은 `plan-bundle`의 bytes를 사용하고 실행 전후 무결성을 대조합니다.
실행 뒤 `producers`가 바뀌어도 다른 분석/계획을 실행 결과에 붙이지 않습니다.
`analysis_graph_limits`는 A의 `flowdelta.graph.limits`를 그대로 전달하며 미제공 시 null입니다.

독립적인 정상 업무 검사는 manifest와 실행 기록을 먼저 검증한 뒤 artifact kind 참조로 관측을
읽습니다. 응답·초기 상태·정리 외에 주문 ID와 `RETURNED` 여부도 확인합니다. 이벤트 문서
최상위의 `run_id`와 `collection_status=COMPLETE`를 실행 기록과 대조하고, 실행이
`phase=FINISHED`이며 종료 시각이 있어야 통과합니다. 파일 해시를 다시 맞춰도 다른 실행의
이벤트 문서·미완료 수집·종료 전 실행을 정상 업무 성공으로 받지 않습니다.

```powershell
uv run --frozen --extra baseline --extra knowledge python tools/check_profiled_order.py var/pipeline-execute --out var/normal-checks.json
```

게시된 [과거 실행 영수증](../examples/module-outputs/profiled-runner/README.md)은 기록된
소스와의 일치를 별도로 확인합니다. 기본 테스트는 현재 작업 트리와 과거 영수증의 소스
동일성을 요구하지 않습니다. 소스 파일을 고쳤다는 이유만으로 과거 실행을 다시 만들 필요는
없으며, 현재 구현의 실행 성공이 필요한 변경은 위 명령으로 새 실행·검증 결과를 남깁니다.
Git/고정 커밋이 없는 환경에서는 소스 커밋 대조만 명시적으로 skip하고 영수증 내부 무결성
검사는 유지합니다. 과거 영수증 검사의 통과는 현재 코드의 Docker 실행 성공이 아닙니다.

Python API는 `runner`와 `sleep`을 주입할 수 있어 실제 C 실행 경로의 성공·실패·정리 실패를
Docker 명령 대역으로 재현합니다. 이 테스트와 실제 Docker 실행 기록은 구분합니다.

**EXECUTED는 등록 검사의 수행 상태입니다.** runner는 사례별 정상 업무 정답이나 보안 위반을
추측하지 않으며 `security_outcome=null`을 유지합니다. 독립적인 기대 결과/관측 판정이 필요하면
별도 소비자에서 수행해야 합니다. 원본 bytes가 포함된 bundle은 공개 전 별도 검토가 필요합니다.

카드의 외부 source_ref는 생성한 analysis bundle 안에서 검증합니다. 이 runner는 임의 외부
artifact를 재귀 수집하지 않습니다. 공용 DB, 선정 n8n-claw의 정책 binding/AI 연결 의미,
새 C 검사 DTO와 실제 LLM 실행 지원은 이번 범용 호출 경로만으로 완성되지 않습니다.
