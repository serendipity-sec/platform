# Issue #79 공용 PostgreSQL 교차 호스트 검증 계획

> 이 문서는 구현 계획이다. 실제 계약과 이슈·PR 상태를 다시 확인한 뒤 실행한다. 계획 작성 과정에서 제품 코드, DB, AWS 자원, GitHub 이슈는 변경하지 않았다.

**목표:** 선정된 n8n-claw 사례 한 건의 입력, A 분석, B 계획, C 결과를 공용 PostgreSQL의 실제 저장소를 통해 정확한 ID로 조회하고, 두 번째 호스트에서 계약이 요구하는 원본 바이트를 확보해 신뢰 가능한 SHA-256과 크기로 검증한다.

**설계:** PostgreSQL은 각 모듈 소유 repository와 ID 관계의 공용 조회 지점이다. 현재 B 결과는 BYTEA로 유지하고, 선정 사례의 제한된 로컬 파일도 PostgreSQL BYTEA 저장이 가능한지 먼저 검토한다. Git 추적 fixture와 pinned upstream 파일은 기존 출처에서 가져온다. S3는 DB 저장이 크기·운영·백업 요구에 맞지 않을 때만 선택한다.

**대상 범위:** #79의 단일 사례와 공용 PostgreSQL 설정, 모듈별 migration 적용 조정, 실제 사례의 exact-ID 조회, 두 호스트 acceptance 및 운영 문서.

## 원 이슈 범위와 카드 VDB 추가 요구

2026-11-04에 다시 확인한 #79 본문은 공용 PostgreSQL, 모듈 migration/권한, artifact URI·resolver·SHA 검증, 선정 사례의 exact-ID 조회와 다른 호스트의 byte 접근을 요구한다. 카드 벡터 catalog/index와 서로 다른 분석마다 새 query embedding/retrieval/plan을 생성하는 동작은 원문에 명시돼 있지 않다. 이 계획의 Task 2는 사용자가 이번 대화에서 명확히 한 추가 의도를 반영한다. 구현 전 이 요구를 #79 acceptance에 반영하거나 별도 이슈로 연결하고, 범위를 기록해야 한다. 나머지 DB·artifact·교차 호스트 작업은 원 #79 요구를 다룬다.

**설계 문서:** [2026-11-03 Issue #79 공유 PostgreSQL 및 산출물 접근 설계](../specs/2026-11-03-issue-79-shared-postgresql-artifacts-design.md)

## 전역 제약

- 선정 사례는 #78이 요구하는 실제 n8n-claw Memory Delete 실행으로 고정한다. 계약 예시나 CONTRACT_EXAMPLE fixture를 실제 실행 증거로 사용하지 않는다.
- #79의 ID chain을 소유 모듈 계약 그대로 유지한다. scan_id가 실제 계약에 확정되기 전 run_id를 scan_id로 매핑하지 않는다. #79에 필수가 아닌 ID를 chain에 임의로 넣지 않는다.
- 현재 B 저장소의 kb_0001 → kb_0002 → kb_0003 이력을 보존한다. 모듈별 Alembic revision graph/version table을 합치지 않는다.
- 각 모듈의 table, repository, migration을 해당 소유자가 관리한다. #50은 공용 연결·적용 순서 조정 역할이다. 확인되지 않은 입력/A/C 저장소를 B migration에 추가하지 않는다.
- 이미 공용으로 읽을 수 있는 Git/upstream/PG 바이트를 다시 S3에 복제하지 않는다.
- n8n-claw 원본 워크플로와 코드가 포함된 raw Semgrep report는 현재 저장소의 보관·배포 제한을 따른다. 재배포 권한이나 확인된 공유 계약 없이 S3에 올리지 않는다.
- 다운로드한 바이트의 SHA-256을 신뢰 해시로 간주하지 않는다. 기대값은 고정 manifest, 신뢰된 원본 메타데이터 또는 모듈 소유 DB row에서 가져와 비교한다.
- 운영 DB, 운영 AWS 계정, 실제 자격증명으로 acceptance test를 실행하지 않는다. 계획 작성 단계에서는 구현·테스트를 수행하지 않았다.
- NVD/OSV/GHSA 전체 수집, 임의 workflow 실행, 별도 HTTP API는 범위에 추가하지 않는다.

## 확인된 출발점과 의존성

### 구현 착수 재확인 — 2026-11-04

현재 구현 기준은 `f6e2aed`이며 PR #73과 #83이 병합된 main을 이 작업 브랜치에 반영했다.
아래 2026-11-03 표는 계획 당시 기록이다. 구현 시에는 이 재확인 결과가 우선한다.

- PR #73: MERGED. `graph/database.py`의 `DatabaseAnalysisRepository.get(analysis_id)`와
  `migrate_analyses()`를 재사용한다. `graph_0001` / `alembic_version_graph`는 B와 독립이다.
  현재 A는 명시적으로 기존 `KnowledgeSettings`와 `engine_for`를 재사용하므로
  `FLOWDELTA_KB_DATABASE_URL` / `FLOWDELTA_KB_MIGRATION_URL`이 A에도 적용된다.
- PR #83: MERGED. 등록 정적 adapter는 존재하지만 `StaticScanRun`의 실제 식별자는
  `run_id`이며 `scan_id` 필드는 없다. C 공유 DB repository는 아직 없고 정적 catalog와
  B `Template`/`TestPlan` 연결 계약은 `docs/STATIC_SCAN.md`의 합의 항목으로 남아 있다.
- PR #58/#59와 #48/#49/#50/#51/#78/#79는 OPEN. 실제 #78 실행의 발행 ID와
  두 물리 호스트 acceptance 기록은 이 checkout에서 확인되지 않았다.
- 추가 카드 VDB 요구는 #79의 2026-11-04 구현 계획 댓글
  `issuecomment-6624420876`에 기록됐다. B catalog 구현은 독립적으로 진행하며,
  C ID를 임의 생성하거나 없는 owner repository를 B가 대신 구현하지 않는다.


상태는 2026-11-03에 확인된 범위다. 구현 시작 직전에 이슈와 PR head/상태를 새로 확인한다.

| 항목 | 확인된 역할/상태 | 계획에서의 취급 |
|---|---|---|
| #79 | OPEN. 공용 PG, migration/order, 원본·artifact 접근과 해시 검증, 단일 사례 exact lineage 검증 요구 | 완료 기준의 출처 |
| #78 | OPEN. 실제 Memory Delete 사례, 등록된 C adapter/catalog 및 실제 ID 요구 | 실제 acceptance 사례와 C 결과 계약의 선행 조건 |
| #48 / #49 | OPEN. 입력·producer 경로와 결과 계약 소유 범위 확인 필요 | 어떤 저장소가 입력 ID와 원본 bytes를 제공하는지 소유자와 확정 |
| #50 | OPEN. 공용 DB 연결/migration 조정 | DB 준비와 독립 module migration 순서 조정 |
| #51 | OPEN. C persistence/result contract의 owner 확인 필요 | 실제 C 결과 exact-ID 조회가 제공되는지 확인 |
| PR #58 | OPEN, CHANGES_REQUESTED. Memory/File 입력 repository 경로 | 공용 PostgreSQL adapter로 간주하지 않음 |
| PR #59 | OPEN, CHANGES_REQUESTED. pipeline runner | 실제 #78 사례를 만드는 데 필요한 의존성인지 확인 |
| PR #69 | MERGED | 이미 반영된 변경을 재구현하지 않음 |
| PR #73 | OPEN, CHANGES_REQUESTED. A graph.analysis BYTEA와 독립 graph migration 제안 | 병합되고 검증되면 A repository로 재사용 |
| PR #83 | OPEN, CHANGES_REQUESTED. C static-scan adapter/catalog | C result repository 또는 shared persistence가 이미 완성됐다고 간주하지 않음 |

현재 근거의 구체 경계:
- experiments/n8n-claw-c/collect.py는 고정된 upstream revision의 파일을 가져오며 SHA-256과 크기를 확인한다. 원본 workflow는 S3로 복제하지 않고 각 호스트에서 기존 pinned-source 경로를 재사용한다.
- 예시 ScenarioCard 파일은 Git에 추적되지만, 현재 flowdelta plan은 --cards 로컬 디렉터리에서 load_cards()로 읽는다. 실행에 사용한 정확한 카드들은 kb-snapshot.json으로 직렬화되어 B 산출물과 함께 PostgreSQL에 저장된다. 이 snapshot은 이미 만들어진 plan의 증거이지 검색 가능한 공유 카드 카탈로그/VDB는 아니다. pgvector extension은 설치되지만 현재 카드 retrieval은 KEYWORD 방식이며 pgvector 색인·검색을 사용하지 않는다. examples/static-scan 결과는 계약 예시이므로 acceptance evidence가 아니다.
- B의 plan/retrieval/KB snapshot bytes는 현재 PostgreSQL BYTEA에 저장되고 plan_id로 복원하는 기존 repository/export 경로가 있다.
- B의 source_revision은 raw_path, sha256, size_bytes 메타데이터를 두지만 원본 파일은 FLOWDELTA_KB_RAW_ROOT 아래 로컬에서 읽는다. 이 사실만으로 전체 raw root를 교차 호스트에 복사해야 한다고 결론내리지 않는다.
- PR #73의 A BYTEA repository는 아직 병합된 구현이 아니다. 병합 전에는 현재 기능으로 전제하지 않는다.
- C raw Semgrep report에는 원본 코드 줄이 포함될 수 있고 현재 실험은 이를 로컬 var에 제한한다. sanitized output은 raw report bytes와 다른 산출물이다.
- docs PR #40의 artifacts.storage_uri는 설계 참고이지 현재 실행 migration으로 보지 않는다.

## 작업 계획

### Task 0 — 실제 사례, 계약, 필수 바이트 확정

**대상 문서:** docs/KNOWLEDGE_OPERATIONS.md, docs/STATIC_SCAN.md  
**선행:** #48/#49/#51/#78 및 실제 실행에 필요한 #59·PR 상태 확인

- [ ] #78에서 요구하는 Memory Delete 사례의 실제 producer 실행을 선택한다. 실제 입력 ID, A 결과, B retrieval/plan, C 결과와 등록된 adapter/catalog를 추적할 수 있어야 한다.
- [ ] #48/#49 소유자와 input, source revision, case ID, 원본 bytes의 조회 경로를 확정한다. PR #58의 Memory/File 구현만으로 공용 PG 저장소가 제공된다고 가정하지 않는다.
- [ ] #51/#78 소유자와 C 저장소, 실제 결과 식별자, 결과와 plan 사이의 관계를 확정한다. #79의 scan_id가 실제 저장 계약에 아직 없으면 이를 선행 계약 결정으로 남긴다.
- [ ] 사례의 필수 데이터를 한 파일/바이트 단위로 inventory한다. 각 항목에 소유 모듈, 현재 위치, 소비자가 실제로 읽는지, 신뢰 SHA/크기 출처, 교차 호스트 확보 방법, 공유 권한을 기록한다.
- [ ] 파일 출처를 Git 고정 commit, pinned upstream URL, 모듈 소유 PostgreSQL row, producer가 --cards로 넘긴 로컬 ScenarioCard 디렉터리, FLOWDELTA_KB_RAW_ROOT, 로컬 var/generated output으로 구분한다. 단순히 존재하는 파일이 아니라 해당 사례 계약이 요구하는 파일만 필수 목록에 넣는다.
- [ ] 서로 다른 입력 또는 A analysis revision마다 새 retrieval/plan을 생성한다. 단지 비슷해 보인다는 이유로 이전 plan을 재사용하지 않는다. 이미 생성한 plan을 다시 열 때만 그 plan_id의 저장된 kb-snapshot을 사용한다.
- [ ] 새 plan의 카드는 공유 PostgreSQL/pgvector VDB에서 검색하도록 계획한다. 현재 B는 로컬 --cards + KEYWORD 검색이므로, B 소유 card catalog/embedding repository와 migration을 추가하는 구현 작업이 필요하다.
- [ ] 같은 사례의 실제 ID 관계를 확인한다. 별개의 유효 ID들을 섞은 조회를 성공으로 처리하지 않는다.
- [ ] 필수 C raw bytes의 공유 권한이 없으면 raw byte 교차 호스트 검증에만 blocker를 설정한다. raw가 필수 요구가 아니면 공유 대상에서 제외한다. sanitized 대체물은 별도 산출물로 표기한다.
- [ ] 기존 code/schema가 필수 ID·URI를 제공하지 않는 경우, 바로 새 공용 table을 추가하지 말고 해당 owner가 저장 모델과 migration owner를 결정하도록 이슈/PR 분리안을 만든다.

**완료 게이트:** 실제 사례, exact-ID contract, 필수 파일 목록과 bytes 확보 방법이 승인되어야 DB/consumer 구현 단계로 진행한다.

### Task 1 — 공용 PostgreSQL 준비 및 module migration 순서 정리

**주요 문서:** docs/KNOWLEDGE_OPERATIONS.md  
**협업:** #50 및 입력/A/B/C 각 저장소 owner

- [ ] 팀이 이미 선택한 환경을 확인한다. 현재 계획의 기준은 PostgreSQL 17 및 pgvector 0.8.1이며, 운영 접속 정보는 문서나 source control에 기록하지 않는다.
- [ ] DDL/migration 역할과 app 역할, 접속 URL 배포 방식, 필요한 schema/table 권한을 정한다. FLOWDELTA_KB_DATABASE_URL과 FLOWDELTA_KB_MIGRATION_URL은 현재 B 설정이므로 A/C 설정명으로 재사용한다고 가정하지 않는다.
- [ ] module별 migration tree와 최초 지원 revision을 목록화한다: B의 kb_0001 → kb_0002 → kb_0003, A graph migration은 PR #73 병합 후 실제 상태, 입력/C migration은 각 owner가 제공하는 승인 경로만 기록한다.
- [ ] #50이 module owner가 제공한 migration 명령을 확인된 순서로 문서화한다. revision history와 version table을 하나로 합치지 않는다.
- [ ] 필요한 소유 모듈 테스트와 전용 PostgreSQL에서 빈 DB 적용, 지원되는 기존 revision 업그레이드, 두 번째 적용, app role 권한을 검증한다.
- [ ] 기존 테이블에 새 migration이 필요하다는 계약 증거가 없으면 artifact metadata/lineage 테이블을 만들지 않는다.

**검증 범위:** B는 기존 tests/knowledge/test_database.py와 repository 검증을 재사용한다. A 및 C는 각 owner PR이 병합된 뒤 그 모듈의 migration/repository 테스트를 사용한다. 운영 DB에는 적용하지 않는다.

### Task 2 — 공유 VDB 카드 검색 및 exact-ID lineage 연결

**파일 범위:** 확인된 owner repository와 #79 consumer/integration 경로. 의존성 구현이 병합되기 전에 같은 기능을 중복 작성하지 않는다.

- [ ] 입력과 source ID는 #48/#49의 실제 저장소 계약으로 조회한다. PR #58이 제공하는 경로는 그 기능 범위로만 사용한다.
- [ ] B의 ScenarioCard catalog는 카드 JSON, version, provenance, canonical content hash를 보존하는 B 소유 저장소로 관리한다. 적절한 B knowledge migration/repository를 추가하되 generic artifact table은 만들지 않는다.
- [ ] 카드 식별·중복 기준은 (card_id, version)으로 둔다. CVE는 선택적인 evidence metadata이며 dedup key로 쓰지 않는다. 같은 CVE에 서로 다른 revision·환경·증거를 설명하는 카드가 공존할 수 있고, 벡터 유사도만으로 카드를 병합하지 않는다. 동일 identity에 다른 canonical hash가 들어오면 갱신/충돌 정책을 명시한다.
- [ ] 카드가 새로 등록되거나 content hash 또는 embedding profile이 바뀔 때 해당 카드 벡터를 생성·갱신한다. 동일 카드·동일 profile의 임베딩은 매 plan마다 다시 만들지 않는다. 모델/profile ID, dimension, hash를 저장해 서로 다른 벡터 공간을 섞지 않는다.
- [ ] 서로 다른 input/A analysis마다 query embedding을 생성하고 같은 profile의 pgvector 인덱스에서 유사 카드 후보를 조회한다. 유사도 점수는 후보 관련도이며 무결성 검증이나 위험도 판정이 아니다. runtime/version/resource/node 조건, card 상태, C template 등록 여부 등 명시적 eligibility 규칙을 적용한다.
- [ ] B는 선택된 card_id/version, content hash, similarity score, embedding profile과 적용/제외 사유를 retrieval 결과에 남기고, 정확한 카드 bytes를 kb-snapshot으로 고정한다. 이 snapshot과 retrieval/plan은 정확한 plan_id로 PostgreSQL에서 다시 읽는다.
- [ ] A analysis는 PR #73이 병합·검증된 경우에만 A 소유 DatabaseAnalysisRepository 및 정확한 analysis_id로 조회한다.
- [ ] B retrieval/plan/kb-snapshot은 B repository의 정확한 plan_id 경로로 조회한다. 이미 저장된 plan을 다른 호스트가 읽을 때 새 임베딩 검색을 반복하지 않는다. B BYTEA를 S3나 새로운 generic artifact table로 옮기지 않는다.
- [ ] C 결과는 #51이 제공하고 #78 사례에서 실제 검증한 저장소·식별자 계약으로 조회한다. PR #83의 static adapter/catalog을 result persistence로 간주하지 않는다.
- [ ] consumer는 case_id에서 #79가 요구한 각 exact ID로 연결을 따라가고, 각 결과가 동일 실제 사례의 관계인지 확인한다. latest-for-case 선택은 금지한다.
- [ ] 미등록 ID, 존재하지 않는 ID, 서로 다른 사례의 ID 조합, 허용되지 않은 상태와 결과 종류는 분명한 실패로 처리한다.
- [ ] 검증은 서로 비슷하지만 내용이 다른 두 analysis가 서로 다른 retrieval/plan을 만들고, 같은 카드/profile 임베딩은 불필요하게 재생성되지 않는지 포함한다. 카드 내용 hash/profile mismatch는 분리한다.
- [ ] 필요하면 기존 owner repository test에 계약 검증을 추가하고, 소유 기능을 한 경로로 확인하는 제한된 통합 테스트를 둔다. 새 저장 table이나 CLI는 owner migration/interface 결정 후 추가한다.

**완료 게이트:** 입력 → A analysis → B retrieval/plan → 실제 C 결과가 소유자 계약의 ID와 관계대로 한 사례에 연결되고, consumer가 각 결과를 정확히 조회한다.

### Task 3 — producer에서 consumer까지 산출물 왕복 완성

이 작업은 파일을 공유 위치에 놓는 단계와 다른 호스트가 이를 읽는 단계를 하나의 end-to-end 흐름으로 다룬다. 같은 선정 사례와 exact ID를 사용해 생산, 참조 저장, 소비, 바이트 검증을 연결한다.

**협업:** 입력/A/B/C owner, #50 DB 운영 담당자, #78 사례 담당자
**문서:** docs/KNOWLEDGE_OPERATIONS.md, 필요 시 docs/STATIC_SCAN.md

#### 3.1 필수 데이터와 출처 확정

- [ ] 선정 사례 consumer가 실제로 필요한 파일만 목록화하고 각 파일에 source, owner, trusted SHA/size, 권한, 두 번째 호스트 접근 경로를 기록한다.
- [ ] n8n workflow는 experiments/n8n-claw-c/sources.json의 고정 URL/commit/SHA/size를 사용해 collect.py로 가져온다. Git 추적 fixture는 고정 commit에서 읽는다.
- [ ] ScenarioCard catalog/vector는 공유 PostgreSQL/pgvector에서 검색한다. 이미 생성된 plan에 쓰인 정확한 카드들은 B의 PostgreSQL kb-snapshot으로 읽는다.
- [ ] B plan/retrieval/snapshot은 exact plan_id로 현재 BYTEA repository에서 가져온다. export-plan은 B output 복원이지 전체 A→B→C bundle 복원이 아니다.
- [ ] A/C 결과는 각각 owner repository와 exact ID로 읽는다. PR #73/#51 등 선행 기능이 제공되기 전에는 해당 경로를 구현 완료로 가정하지 않는다.
- [ ] B source_revision의 로컬 raw 파일은 사례 계약이 실제로 요구할 때만 교차 호스트 항목으로 포함한다. pinned upstream 재수집으로 해결되지 않는 제한된 파일이면 PostgreSQL BYTEA 저장을 우선 검토하고, DB가 부적합할 때 S3를 검토한다.
- [ ] C raw Semgrep report를 요구하면 공유 권한과 범위를 확인한다. 권한이 없으면 raw byte 공유를 중단 조건으로 기록하고, sanitized 산출물을 raw report와 동일한 바이트로 취급하지 않는다.

#### 3.2 생산자 측 저장과 참조 발행

- [ ] B BYTEA 결과와 카드 snapshot은 현재 B repository/migration을 사용한다. 다른 Git/PG 바이트를 중복 업로드하지 않는다.
- [ ] 공유가 필요한 제한된 local-only 파일은 S3보다 먼저 PostgreSQL BYTEA 저장 가능성을 검토한다. 적용한다면 owner가 artifact ID, 바이트, trusted SHA/크기, 사례 관계를 저장할 테이블·repository·migration을 정하고, consumer가 exact ID로 바이트를 읽게 한다. 기존 B output 전용 테이블에 임의 파일을 끼워 넣거나 B가 다른 모듈 migration을 소유하지 않는다.
- [ ] 이 DB 경로는 S3 URI 없이도 artifact ID로 바이트를 조회·검증할 수 있다. #79 owner가 URI 필드를 명시적으로 요구하면 DB artifact ID가 그 참조 계약을 충족하는지 먼저 합의한다.
- [ ] 파일 크기·건수, DB 용량, 백업/복구 요구 또는 팀 표준상 BYTEA가 부적합한 경우에만 S3 backend를 선택한다. S3를 선택하면 권한 확인된 파일만 지정 bucket/prefix에 올리고 trusted SHA/크기를 대조한다. 객체 업로드 확인 후 PostgreSQL URI 참조를 발행해 dangling URI를 피한다.
- [ ] S3 Python 전송 코드는 승인된 설계가 직접 SDK 호출을 요구할 때 boto3를 사용한다. private bucket, 최소 권한 role, short-lived credentials를 쓰고, Git/PG/pinned URL로 이미 얻을 수 있는 파일은 복제하지 않는다.

#### 3.3 소비자 측 exact-ID 조회와 바이트 검증

- [ ] 다른 호스트의 consumer는 case_id에서 실제 소유 계약의 analysis_id, retrieval_id/plan_id, C 결과 ID를 따라간다. scan_id는 C 계약에 필드가 확정된 경우에만 사용하고 run_id를 대신 넣지 않는다.
- [ ] URI, BYTEA, pinned URL 중 각 데이터의 정해진 source에서 바이트를 가져온다. 기대 SHA/크기는 신뢰된 manifest나 owner DB metadata에서 얻어 받은 바이트와 비교한다.
- [ ] 알 수 없는 ID, 다른 사례 ID 혼합, missing/denied object, 잘못된 해시·크기일 때 실패시키고 검증 전 부분 파일을 소비자에게 노출하지 않는다.
- [ ] 같은 사례의 실제 lineage, 선택 카드 snapshot, B plan 및 C 결과가 서로 연결되는지 확인한다. 유사한 입력은 새 retrieval/plan을 만들고, 과거 plan 조회는 저장된 exact snapshot을 사용한다.

#### 3.4 실제 교차 호스트 acceptance와 운영 인계

- [ ] 실제 #78 사례를 producer host에서 생성·저장하고, 물리적으로 다른 consumer host와 별도 팀원 계정에서 공용 PostgreSQL의 exact ID lineage를 조회한다.
- [ ] 소비 호스트에서 필수 bytes를 원래 source 경로로 가져와 SHA-256/크기를 검증한다. S3 branch가 승인된 경우에만 두 host role의 object 접근과 S3 실패 경로도 확인한다.
- [ ] 지원 migration의 empty DB/upgrade/repeat 및 app role 최소 권한은 Task 1의 전용 DB 검증을 완료 조건으로 연결한다. unit fake나 한 호스트의 서로 다른 계정만으로 실제 두 호스트 acceptance를 대신하지 않는다.
- [ ] 실제 입력과 비슷하지만 내용이 다른 분석 두 건이 서로 다른 retrieval/plan을 만드는지 확인한다. 카드 임베딩은 같은 content hash/profile에 대해 재사용하고 카드 내용/profile 변경 시 갱신한다.
- [ ] docs/KNOWLEDGE_OPERATIONS.md에 접속, owner별 migration 순서, 카드 catalog 갱신, 새 분석 plan 생성, 과거 plan 조회, artifact source, SHA/크기 확인, 복구 절차를 기록한다. 선택적 S3가 실제 사용될 때만 bucket/role/lifecycle/비용 항목을 추가한다.
- [ ] static output, 실제 C 결과, 원본 바이트 조회, 실제 두 호스트 검증은 각각 구분해 완료 기록을 남긴다. 계약 예시나 제한된 replay를 full n8n/LLM 실행으로 표시하지 않는다.

**완료 게이트:** 실제 사례 producer가 만든 DB 참조와 bytes를 다른 물리 호스트가 exact ID로 찾아 읽고, 같은 사례 관계와 trusted SHA/크기를 확인한다. S3는 필수 local-only 공유 가능 파일이 확인된 경우에만 이 왕복 경로에 포함한다.

## 최종 완료 조건

- #78의 실제 사례와 모든 exact ID의 계약상 관계가 확정됐고, 실제 consumer가 “최신” 조회 없이 그 사례를 연결한다.
- 각 소유 모듈 migration이 독립 이력으로 지원 상태를 통과하며, 공용 DB에서 빈 설치·지원 버전 upgrade·반복 적용과 app 권한이 검증된다.
- 두 번째 호스트가 실제 결과를 exact ID로 조회하고 계약상 필수 원본/산출물 bytes를 얻어 신뢰된 기대 SHA-256과 크기를 확인한다.
- 공유하지 않아도 되는 파일을 S3에 복제하지 않는다. S3가 필요한 경우에는 권한, 저장 owner, dependency PR 및 acceptance가 계획에 포함된다.
- 운영 문서만으로 접속, migration, lineage 조회, bytes 확보, 무결성 실패 복구를 수행할 수 있다.

## 구현 착수 전 재확인

이슈와 PR은 서로 다른 시점에 바뀔 수 있다. 구현 전 최신 #48/#49/#50/#51/#78 및 PR #58/#59/#73/#83 상태, 소유 계약, 실제 사례 ID를 확인한다. OPEN/CHANGES_REQUESTED 상태 자체를 모두 blocker로 취급하지 말고 각 deliverable에 필요한 기능의 실제 병합 여부만 gate로 적용한다.
