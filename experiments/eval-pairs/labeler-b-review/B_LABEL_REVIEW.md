# B 라벨링 — replay 범위

**36개 라벨 작성: 위반 확정 8, 위반 없음 25, 근거 부족 3.** 양유상 검토용 AI 보조 결과이며 사람 검토 완료는 아니다.

기준은 공개 README가 지정한 `experiments/n8n-claw-memory/evidence/observations.json`의 실제 수신부 요청·초기/최종 상태다. `expected_deleted`, `expectation_match`, `passed`, `observation` 요약을 정답으로 사용하지 않았다. A 원문·생성기는 열거나 실행하지 않았다. 제품 judge 미실행은 11/12 최초 작성 이력이며, 11/16 수정 담당의 제품 구현·테스트 노출은 [독립성 기록](INDEPENDENCE.md)에 추가했다.

| 쌍 | 쪽 | 본인 삭제 | 타인 삭제 | 관리자 삭제 | 근거 적용 방식 |
|---|---|---|---|---|---|
| R1 | 전 | 근거 부족 | 근거 부족 | 근거 부족 | DIRECT_REPLAY_RECORD |
| R1 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| F1 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| F1 | 후 | 위반 없음 | 위반 없음 (`BLOCKED`) | 위반 없음 | DIRECT_REPLAY_RECORD |
| K1 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K1 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_WORKFLOW_IDENTITY |
| K2 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| K2 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K3 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K3 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| N1 | 전 | 위반 없음 | 위반 없음 (`BLOCKED`) | 위반 없음 | REPLAY_BY_WORKFLOW_IDENTITY |
| N1 | 후 | 위반 없음 | 위반 없음 (`BLOCKED`) | 위반 없음 | REPLAY_BY_CODE_IDENTITY |

근거 적용 방식:

- `DIRECT_REPLAY_RECORD` 15개: 동일 upstream revision의 공개 replay 기록.
- `REPLAY_BY_WORKFLOW_IDENTITY` 6개: revision은 다르지만 원본 workflow 전체 바이트가 같은 기록.
- `REPLAY_BY_CODE_IDENTITY` 15개: 선택한 Memory Delete JS가 바이트까지 같고, 동일한 합성 입력·호출자·수신부를 적용한 기록. 대상 workflow 전체를 실행했다는 뜻이 아니다.

타인 삭제 8개에서는 일반 호출자와 객체 소유자가 다르고 관리자 목록이 비어 있는데, 합성 수신부의 DELETE가 한 행을 제거했으며 최종 상태에서도 ID 8이 없어졌다. 본인·관리자 삭제는 같은 삭제 효과가 정책상 허용된다. 보완 코드의 타인 요청은 두 GET 뒤 거절했고 DELETE가 없으며 객체가 남았다. R1 변경 전 3개는 선택 도구가 없어 `NOT_RUN`이므로 근거 부족이다.

허용 정책은 ‘일반 사용자는 본인 개인 기억만 삭제, 관리자는 타인 개인 기억도 삭제 가능’이다. 호출자는 `web:fixture-a`, 대상은 ID 8이다. 본인 경우 소유자 A/관리자 목록 빈 값, 타인 경우 소유자 B/빈 값, 관리자 경우 소유자 B/관리자 목록 A로 고정돼 있다.

‘위반 없음’은 **정직한 합성 호출자와 합성 수신부에서 선택 도구 JS를 실행한 범위**의 라벨이다. 전체 n8n+LLM, 실제 DB, 인증 경계, 다른 노드와 연쇄 효과는 `NOT_RUN`이다. N1의 다른 노드 변경도 전체 실행으로 검증하지 않았다. R1 전쪽의 도구 부재를 전체 workflow의 안전 기준선으로 쓰지 않는다.

36개 슬롯은 12개 원본 관측을 직접 또는 바이트 동일성으로 재사용한다. 반복 실행·독립 표본 수로 세지 않으며 개발 계열 결과로만 둔다. 사례별 원본 JSON pointer·해시·정책·전후 상태·판단 이유는 [labels-B.json](labels-B.json)에 있다.

11/16 v2에서는 [최신 docs #70 초안](sources/rules-70-20261114.md)에 따라 세부 판정과 상위
라벨을 분리했다. 위반 8, 비관측 22, 차단 3, 근거 부족 3이다. 모든 사례에 `replay_scope`,
고정 정책 출처를 가진 `authorization_context.memory_admins`, 원본 `source_execution_status`와
계약용 `execution_status`를 넣었다. `COMPLETED → SUCCEEDED`는 선택 JS replay에만 적용한다.

관리자 예외는 정책대로 유지하지만 현재 제품 판정 입력에는 관리자 정책 문맥과 replay 어댑터가
없어 직접 투입할 수 없다. 이 미지원 상태를 명시적으로 거부하며, 제품 예측에 맞춰 라벨을 바꾸지
않는다. 상세 형식·검증 방법·v1 보존 위치는 [REVIEW_CONTRACT.md](REVIEW_CONTRACT.md)에 있다.
