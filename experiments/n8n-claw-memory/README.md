# n8n-claw Memory Delete 근거와 재현

[#48 선정 기록](https://github.com/serendipity-sec/platform/issues/48#issuecomment-6257456324)의
공용 인계 자료다. 공급받은 workflow에 삭제 도구가 추가됐고, 요청자와 대상 기억 소유자를
비교하지 않는 코드가 발견·보완·되돌림된 이력을 다룬다.

## 팀원이 재현하는 방법

Python 3.13와 실행 중인 Docker가 필요하다. 저장소 루트에서:

```powershell
uv run --frozen python experiments/n8n-claw-memory/collect.py --out var/claw-sources
uv run --frozen python experiments/n8n-claw-memory/run.py --sources var/claw-sources --out var/claw-replay
```

첫 명령은 sources.json의 고정 원문 8개만 내려받고 SHA-256/크기를 확인한다. 재실행 시
기존 파일의 해시를 검증하며 덮어쓰지 않는다. 다운로드는 fsync 후 원자적으로 공개하므로
실패한 부분 파일 때문에 재시도가 막히지 않는다. 수집기와 source manifest 해시도 남긴다.
수집·실행 경로는 이 저장소의 `var/` 하위로 제한하며 symlink/junction을 거부한다.
원본 URL은 팀 전체가 조회할 수 있는 공개
commit URL이다. 별도 LICENSE가 없어 원본 코드의 재배포를 유보한 근거는
[THIRD_PARTY.md](../../THIRD_PARTY.md)에 있다. `var`의 원본은 커밋하지 않는다.

두 번째 명령은 먼저 파일 무결성을 다시 검사하고, 이미 로컬에 있는 고정 n8n 이미지에서
네트워크 없음·읽기 전용·권한 제거·메모리/PID 제한·90초 timeout으로 실행한다.
선택한 도구 호출은 VM 안에서 시작하여 동기 timeout을 적용하고, 별도 worker의 deadline으로
비동기 무한 반복과 미완료 Promise도 종료한다. 사용자 중단·timeout 모두 컨테이너 정리를 시도하고
정리 실패는 원래 실행 실패와 별도로 기록한다.
이미지가 없으면 공식 이미지의 아래 digest를 별도로 준비한다. 스크립트가 임의의 이미지를
자동 pull하거나 원본을 호스트 Node/Python 프로세스에서 실행하지 않는다.

이미지는 `runtime/order-lookup/runtime.lock.toml`에서 읽으며 receipt에는 고정 digest와
로컬에서 확인한 실제 image ID를 구분해 기록한다.

## 결과를 읽는 법

- `observations.json`: 원본 Memory Delete JS를 동일한 합성 HTTP 수신부·사용자 문맥에서
  실행한 7개 버전 × 타인/본인/관리자 3개 조합. 추가 전 3개는 선택한 노드 ID의 부재 확인이다.
  `expected_deleted`와 실제 `deleted`를 분리한다. 불일치·장치 오류·호출 예외도 버리지 않으며
  `passed=false`로 남긴다. 거절 이유는 OWNERSHIP_DENIED 등 정규화된 코드와 원문 해시로 보존한다.
  `passed`는 실험 예상과 관측의 일치이며, 삭제가 차단됐다거나 보안상 안전하다는 뜻이 아니다.
- `author-tests.summary.json`: 원저자 테스트의 성공 수/전체 수/반환 코드와 원시 로그 해시.
  테스트 제목 등 원문 표현을 포함하는 stdout/stderr는 로컬 `var/`에만 둔다.
- `receipt.json`: 소스/harness/실행기/수집기/runtime lock 해시, 실제 이미지, 실행·정리 상태,
  반환 코드와 결과 파일 해시. 원시 stdout 해시와 정제해 공개한 파일 해시는 구분한다.
- 공유된 `evidence/`는 이 명령으로 생성한 결과 중 원문 코드·DB·자격을 제외한 자료다.

관측 대상은 `memory_long`의 id=8 합성 행이다. 일반 사용자는 본인 기억만 삭제할 수 있고
관리자는 타인 기억을 삭제할 수 있다는 연구 정책을 사용한다. HTTP 수신부는 실제 DB가 아니다.
원본 코드의 반환값뿐 아니라 합성 수신부에서 DELETE가 발생했는지 따로 기록한다.
모든 HTTP·문맥 조회 시도를 검사 전에 기록하므로 원본 try/catch가 장치의 오류를 삼킬 수 없다.
수신부는 id/owner/scope/entity의 eq와 is.null, 명시적 select, return=minimal/representation만
지원하며 해당 필터를 실제 행 선택에 적용한다. 지원 밖 요청은 상태를 바꾸지 않고 장치 오류로 남긴다.
원본 workflow의 임의 네트워크 목적지나 실제 계정에는 접속하지 않는다.

| 비교 | 원본 쌍 | 의미 |
|---|---|---|
| 기능 추가 | `9ebf36b` → `63a7f2e` | 전용 기억 수정·삭제 도구와 AI 연결 추가 |
| 첫 보완 | `4d1b1b7` → `eb6d7bc` | 소유자·관리자 검사 추가 |
| 되돌림 | `eb6d7bc` → `2c6df44` | 보완 되돌림 후 타인 삭제 재발 |
| 후속 보완 | `c71b39e` → `fe57d16` | 추가 정책·감사·테스트; 첫 복구 시점으로 해석하지 않음 |

4월의 기능 추가와 7월의 보완을 하나의 최소 변경 쌍으로 합치지 않는다. 실제 보완에는
DB·설정 변경도 포함되므로 MVP 파생 사례에서는 고정 공통 스키마와 변형 기록이 필요하다.

## 검증 경계와 다음 인계

현재 근거는 **도구 JS 부분 실행**이다. 전체 n8n+LLM, 실제 DB, C 등록 검사, 공격 성공률,
A→B→C 및 공용 DB 왕복은 확인하지 않는다. 추가 전 범용 HTTP Tool도 있으므로 이전 workflow
전체를 안전하다고 라벨링하지 않는다. `NOT_RUN`과 원본 `UNKNOWN` 상태를 유지한다.

2026-10-24 리뷰 보완 후 21개 관측과 저자 테스트 21개를 다시 실행했다.
팀 합성 회귀 검사 `test-replay.cjs`는 예상 불일치, 삼켜진 예외, 소유자 필터, 반환 형식,
동기/비동기 무한 반복을 같은 제한 컨테이너에서 검사한다. Python 회귀 검사는
`uv run --frozen pytest tests/test_memory_experiment.py`로 실행한다.

- 입력 #48: 고정 profile에 공통 환경·모델·사용자·상태를 연결하고 원본/파생물 차이를 기록.
- A #49: `tool-memory-delete`/`tool-memory-update`의 `ai_tool` 관계를 CONTROL과 구분.
- B #50: CVE가 없는 공개 발견/보완 commit을 근거로 수용하고 정확한 case/analysis ID로 검색.
- C #51: `agent@3.1`, `toolCode@1.2`, `code@2`의 지원 범위와 검사 DTO를 확정.
  실제 agent 관측은 별도의 승인된 실행 환경과 독립 상태 확인이 필요하다.
