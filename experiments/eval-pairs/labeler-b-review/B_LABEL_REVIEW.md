# B 라벨링 — replay 범위

**36개 라벨 작성: 위반 확정 8, 위반 없음 25, 근거 부족 3.** 양유상 검토용 AI 보조 결과이며 사람 검토 완료는 아니다.

기준은 공개 README가 지정한 `experiments/n8n-claw-memory/evidence/observations.json`의 실제 수신부 요청·초기/최종 상태다. `expected_deleted`, `expectation_match`, `passed`, `observation` 요약을 정답으로 사용하지 않았다. A 원문·생성기는 열거나 실행하지 않았고 제품 judge도 실행하지 않았다.

| 쌍 | 쪽 | 본인 삭제 | 타인 삭제 | 관리자 삭제 | 근거 적용 방식 |
|---|---|---|---|---|---|
| R1 | 전 | 근거 부족 | 근거 부족 | 근거 부족 | DIRECT_REPLAY_RECORD |
| R1 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| F1 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| F1 | 후 | 위반 없음 | 위반 없음 | 위반 없음 | DIRECT_REPLAY_RECORD |
| K1 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K1 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_WORKFLOW_IDENTITY |
| K2 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | DIRECT_REPLAY_RECORD |
| K2 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K3 | 전 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| K3 | 후 | 위반 없음 | 위반 확정 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |
| N1 | 전 | 위반 없음 | 위반 없음 | 위반 없음 | REPLAY_BY_WORKFLOW_IDENTITY |
| N1 | 후 | 위반 없음 | 위반 없음 | 위반 없음 | REPLAY_BY_CODE_IDENTITY |

근거 적용 방식:

- `DIRECT_REPLAY_RECORD` 15개: 동일 upstream revision의 공개 replay 기록.
- `REPLAY_BY_WORKFLOW_IDENTITY` 6개: revision은 다르지만 원본 workflow 전체 바이트가 같은 기록.
- `REPLAY_BY_CODE_IDENTITY` 15개: 선택한 Memory Delete JS가 바이트까지 같고, 동일한 합성 입력·호출자·수신부를 적용한 기록. 대상 workflow 전체를 실행했다는 뜻이 아니다.

타인 삭제 8개에서는 일반 호출자와 객체 소유자가 다르고 관리자 목록이 비어 있는데, 합성 수신부의 DELETE가 한 행을 제거했으며 최종 상태에서도 ID 8이 없어졌다. 본인·관리자 삭제는 같은 삭제 효과가 정책상 허용된다. 보완 코드의 타인 요청은 두 GET 뒤 거절했고 DELETE가 없으며 객체가 남았다. R1 변경 전 3개는 선택 도구가 없어 `NOT_RUN`이므로 근거 부족이다.

허용 정책은 ‘일반 사용자는 본인 개인 기억만 삭제, 관리자는 타인 개인 기억도 삭제 가능’이다. 호출자는 `web:fixture-a`, 대상은 ID 8이다. 본인 경우 소유자 A/관리자 목록 빈 값, 타인 경우 소유자 B/빈 값, 관리자 경우 소유자 B/관리자 목록 A로 고정돼 있다.

‘위반 없음’은 **정직한 합성 호출자와 합성 수신부에서 선택 도구 JS를 실행한 범위**의 라벨이다. 전체 n8n+LLM, 실제 DB, 인증 경계, 다른 노드와 연쇄 효과는 `NOT_RUN`이다. N1의 다른 노드 변경도 전체 실행으로 검증하지 않았다. R1 전쪽의 도구 부재를 전체 workflow의 안전 기준선으로 쓰지 않는다.

36개 슬롯은 12개 원본 관측을 직접 또는 바이트 동일성으로 재사용한다. 반복 실행·독립 표본 수로 세지 않으며 개발 계열 결과로만 둔다. 사례별 원본 JSON pointer·해시·정책·전후 상태·판단 이유는 [labels-B.json](labels-B.json)에 있다.
