# A·RAG 시험 계획

Refs #121. 공통 runner의 고정 공유 검색 연결이며 생성 모델 호출은 아직 하지 않는다.

`run-pipeline ... --planning-config config.json`으로 공유 검색을 선택한다. 기존 옵션은 그대로
사용한다. VECTOR 모드에서는 `--cards` 디렉터리를 읽지 않으며, DB의 승인·색인된 카드를 읽는다.

```json
{
  "schema_version": "0.4.0",
  "config_format": "flowdelta-planning-config-v1",
  "retrieval": "VECTOR",
  "embedding_profile_id": "고정한 profile_id",
  "search_limit": 50,
  "frozen_cards": [
    {"card_id": "카드 ID", "version": "1.0.0", "sha256": "canonical 카드 bytes의 SHA-256"}
  ]
}
```

DB 연결과 임베딩 endpoint는 기존 `FLOWDELTA_KB_DATABASE_URL`, `FLOWDELTA_EMBEDDING_*` 설정을
사용한다. 설치·색인은 [임베딩 안내](EMBEDDING.md)를 따른다. runner가 DB migration·카드 색인·모델
다운로드를 자동 수행하지 않는다. 고정 카드 누락·해시 변경·profile 불일치는 실패다.

검색은 고정 카드 집합을 **순위 계산 전에** 제한한다. 반환 KB snapshot·검색 근거·profile·query
해시가 plan과 bundle에 연결된다. 요약에는 실제 생성 모델 호출이 없음을 남긴다. 빈 검색은 기존
정상 시험 fallback과 함께 `retrieval_empty=true`로 보존하며 검색·DB 장애와 구분한다.

임베딩 QUERY에는 변경 값, 노드 종류, 업무 목적·성공 조건·보호 자원, 정책 조건과 A 경로의 지원
상태·한계를 전달한다. case 이름·정답·기대 판정·analysis/snapshot ID와 해시는 전송하지 않는다.
기존 민감 필드명 redaction은 heuristic이다. 실제 원문 전송 범위는 이 필드들이며, 검색 endpoint는
호출자가 신뢰하는 서버로 설정한다. 추적 ID는 로컬 검색 산출물에 보존한다.
