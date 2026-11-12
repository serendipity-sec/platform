# 평가 비교쌍 후보 — n8n-claw 공개 이력

Refs docs #28 (#67 2-1·2-2). 평가에 쓸 전후 비교쌍을 n8n-claw의 실제 공개 이력에서 고른 **후보 목록**이다.
정답 라벨은 아직 없다. 라벨은 docs #53 판정 계약으로 붙이고 다른 팀원이 검토한다.

## 재현

```bash
python experiments/eval-pairs/scan_history.py --out experiments/eval-pairs/evidence/claw-history-pairs.json
```

- 원저자 기본 브랜치에서 `workflows/n8n-claw-agent.json`을 바꾼 commit(2026-03-31~07-31, 47개)마다 그 commit과 첫 부모의 파일을 받아 비교한다.
- GitHub API는 `gh` CLI 인증이나 `GITHUB_TOKEN`을 쓴다. 표준 라이브러리만 쓴다.
- 원본 저장소에 LICENSE가 없어 workflow 내용은 저장하지 않는다. commit·날짜·파일 해시·기억 도구 코드 해시·노드 이름·개수만 남긴다.
- [`pairs.json`](pairs.json)은 위 결과에서 고른 후보이고, 파일 해시는 결과 파일에서 그대로 옮겼다.

## 후보

모두 부모 → commit 한 번의 변경이다. 이름 붙인 버전은 `experiments/n8n-claw-c`의 7개 버전과 같다.

| ID | 후보 유형 | 전 → 후 | 바뀐 것 | 독립 사례 |
|---|---|---|---|---|
| R1 | 위험 생김 | `9ebf36b` → `63a7f2e` (feature) | Memory Delete·Update 도구가 소유자 비교 없이 추가됨 | 예 |
| F1 | 수정 | `4d1b1b7` (before-fix) → `eb6d7bc` (first-fix) | 원저자가 기억 도구 4개에 소유권·관리자 검사를 넣음 | 예 |
| R2 | 위험 생김 | `21212cd` → `2c6df44` (reverted) | F1 검사를 되돌림 | **아니요** — F1의 역방향 |
| K1 | 기존 위험 유지 | `d12a897` → `881af14` | AI Agent 노드만 바뀜 | 예 |
| K2 | 기존 위험 유지 | `63a7f2e` → `83df6d3` | Reminder 노드만 바뀜 | 예 |
| K3 | 기존 위험 유지 | `33f7cfd` → `0a37b6e` | Memory Update 코드만 바뀜(metadata 지원) | 예 |
| N1 | 새 위험 없음 | `bb9f6fa` → `6edfe42` | F1 검사가 있는 상태에서 Format Webhook Input만 바뀜 | 예 |
| N2 | 새 위험 없음 | `c516fbc` → `21212cd` | N1을 되돌림 | **아니요** — N1의 역방향 |

후보 유형의 근거:
- R1·F1: 원저자의 F1 commit 제목("ownership, admin-gated deletes")과 `n8n-claw-c`의 정적 분석(보완 전에는 `owner_user_id`와 요청자를 비교하지 않음).
- K1·K2·K3: 전후의 기억 도구 코드가 R1 after(`63a7f2e`)나 before-fix(`4d1b1b7`)와 바이트가 같다. 단 K3의 before Update 코드는 정적 검사 기록이 없다.
- N1: 전후의 기억 도구 코드가 F1 after와 바이트가 같다.

## 발견한 것

1. **R2와 F1은 같은 쌍을 뒤집은 것이다.** R2 before = F1 after, R2 after = F1 before로 파일 전체가 바이트 단위로 같다.
   `n8n-claw-c`의 "되돌림" 쌍(`eb6d7bc` → `2c6df44`)도 같은 파일 두 개다. 평가에서 F1과 R2를 따로 세면 같은 증거를 두 번 센다.
   N1·N2도 같은 관계다.
2. **후보가 모두 n8n-claw 한 계열이다.** C 정적 규칙은 이 계열의 7개 버전에 맞춰 고쳤다(`n8n-claw-c` README: "모든 표는 개발셋 결과").
   그래서 n8n-claw는 **개발 계열**로만 쓰고, 최종 평가에는 다른 원본 계열이 필요하다. 계열 후보는 아직 없다.
3. **"새 위험 없음"(N1)은 F1 검사가 실제로 막는다는 전제에 달려 있다.** C 규칙 0.6.0은 인증된 요청자 출처를 증명할 수 없어
   F1 이후도 경고한다. N1·F1의 라벨은 동적 판정(#53)으로 정한다.
4. **9월 두 버전(`c71b39e`, `fe57d16`)은 원저자 기본 브랜치의 경로 이력에 없다.** "공개 실제 변경"으로 쓰려면 어느 브랜치에 공개됐는지 먼저 확인한다. 이번 후보에서는 뺐다.

## 라벨러 A 초안 라벨 — 봉인 (2026-11-12)

docs #70은 라벨러 2명이 서로의 라벨을 보지 않고 따로 붙이라고 한다. 그래서 라벨러 A(@vmaca123)의 초안 라벨은
**파일을 커밋하지 않고 해시만 [`labels-A.seal.json`](labels-A.seal.json)에 봉인**했다.
라벨러 B가 같은 사례에 라벨을 낸 뒤 A 파일을 공개하고, 누구나 해시로 내용이 바뀌지 않았음을 확인한다.

```bash
python experiments/n8n-claw-c/collect.py --out var/claw-c
python experiments/eval-pairs/draft_labels.py --created 2026-11-12   # 이미 있으면 덮어쓰지 않고 멈춤
```

- 대상은 독립 후보 6개 × 전후 2쪽 × 3가지 경우(본인 삭제·남의 삭제·관리자 삭제) = 36개 사례다.
- 근거는 `experiments/n8n-claw-memory/evidence/observations.json`의 replay 기록이다. 7개 버전에 없는 쪽은
  Memory Delete 코드가 바이트까지 같은 버전의 결과를 이어받는다(`REPLAY_BY_CODE_IDENTITY`).
- 라벨 값은 docs #70 초안의 `위반 확정`·`위반 없음`·`근거 부족`이다. 판정기(#103)는 돌리지 않았다.
- **층위는 replay다.** 선택한 도구 JS를 합성 호출자와 합성 수신부로 돌린 결과이며, 전체 n8n + LLM 실행과 실제 DB는 모두 `NOT_RUN`이다.
- **보완 코드의 "위반 없음"은 가정에 달려 있다.** 보완된 도구는 호출자를 `Merge Input` 노드 출력에서 읽는데, C 규칙 0.6.0은 이것을 신뢰할 수 있다고 증명하지 못한다. replay는 정직한 합성 호출자를 넣었다.
- 생성 규칙이 같으면 같은 바이트가 나온다. 봉인 해시와 다시 만든 파일의 해시가 같으면 내용이 바뀌지 않은 것이다.

## 남은 일 (docs #28)

- [ ] 각 후보에 업무·정책·초기 상태를 붙인다. 지금은 `n8n-claw-a`의 A 분석용 임시 입력뿐이다.
- [ ] #53 판정 계약이 정해지면 독립 정답 라벨을 붙이고 @bear300811가 검토한다.
- [ ] 평가 계열로 쓸 다른 공개 원본을 찾는다. 없으면 공개 원본에 팀이 만든 변형(출처와 변환을 공개)으로 채우고, 실제 이력과 따로 집계한다.
