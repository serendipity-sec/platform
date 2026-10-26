# A 분석 결과 DB 저장

A의 `analysis.json`(`AnalysisBundle`)을 공용 PostgreSQL에 저장하고 정확한 ID로 조회한다. 파일·메모리 저장소(`flowdelta.graph.repository`)와 같은 인터페이스·규칙을 쓴다. `knowledge` extra가 필요하다.

## 연결과 migration

- 연결 설정은 B의 공용 DB 설정을 그대로 쓴다.
  - `FLOWDELTA_KB_DATABASE_URL`: 앱 역할 `flowdelta_app`. 읽기·추가만 한다.
  - `FLOWDELTA_KB_MIGRATION_URL`: migration 역할
  - 설정과 역할 준비는 `docs/KNOWLEDGE_OPERATIONS.md`를 따른다.
- A는 **자기 migration 사슬과 버전 표**(`public.alembic_version_graph`)를 따로 쓴다. B의 `knowledge` migration과 순서가 섞이지 않으며, pgvector가 필요 없다.

```bash
uv run --frozen --extra knowledge flowdelta analysis-db migrate
uv run --frozen --extra knowledge flowdelta analysis-db store var/a/analysis.json
uv run --frozen --extra knowledge flowdelta analysis-db get <analysis_id> --out var/a/copy.json
```

## 저장 규칙

| 규칙 | 동작 |
|---|---|
| 불변 | 같은 ID·같은 내용은 아무 일도 하지 않는다. 같은 ID·다른 내용은 `ANALYSIS_ID_CONFLICT`로 거부한다 |
| 수정·삭제 금지 | 앱 역할에는 UPDATE·DELETE 권한이 없다. trigger가 migration 역할의 UPDATE·DELETE·TRUNCATE도 막는다 |
| 무결성 | 저장한 바이트의 SHA-256을 함께 두고, 읽을 때마다 다시 확인한다(`ANALYSIS_INTEGRITY_MISMATCH`) |
| 다시 읽을 수 있는 것만 저장 | 저장 전에 한 번 다시 읽어 본다. 읽을 수 없는 분석(예: `JSON_DEPTH_LIMIT`)은 저장하지 않는다 |
| 정확한 조회 | `get(analysis_id)` 또는 `find(case_id, before_snapshot_id, after_snapshot_id)`의 정확한 일치뿐이다. "그 case의 최신 분석"은 고르지 않는다 |

## 테이블 `graph.analysis`

- `analysis_id`(기본 키), `case_id`, `before_snapshot_id`, `after_snapshot_id`: 계약 ID 문자열. 공통 case·snapshot 테이블이 아직 없어 외래 키는 두지 않는다.
- `producer`·`origin`: 분석기 버전과 작성 경계(`MODULE_OUTPUT` 등)
- `sha256`, `payload`(bytea): 저장한 `analysis.json` 바이트 그대로
- `limitations`·`extensions`(jsonb): 조회용 사본. 기준은 `payload`다(상한 표시는 `extensions["flowdelta.graph.limits"]`).

## 테스트

`tests/graph/test_database.py`는 `FLOWDELTA_KB_TEST_DATABASE_URL`(앱 역할)과 `FLOWDELTA_KB_TEST_MIGRATION_URL`이 있을 때 실제 PostgreSQL로 돈다. CI의 `knowledge-postgres` job이 B의 DB 테스트와 함께 실행한다. 행을 지울 수 없으므로 테스트마다 새 분석·case ID를 쓴다.
