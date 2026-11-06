# Issue #79: 공유 PostgreSQL 및 산출물 접근 설계

> 상태: 계획 단계 제안. 구현 전 실제 사례, owner contract, migration을 다시 확인해야 한다.

> 범위 메모: 현재 #79 원문은 공용 DB/artifact access를 명시한다. 카드 VDB 색인과 매 분석별 plan 생성은 이번 대화에서 추가로 명확해진 요구이며, 구현 전에 #79에 기록하거나 별도 이슈로 연결한다.

## 구현 기준 갱신 — 2026-11-04

기준 checkout은 `f6e2aed`다. PR #73/#83은 병합됐으며 아래 계획 당시의
미병합 표현보다 이 기록이 우선한다. A의 `DatabaseAnalysisRepository`와 독립
`graph_0001` / `alembic_version_graph`를 재사용한다. A는 현재 B와 동일한
`KnowledgeSettings` 접속 설정을 사용한다. C의 `StaticScanRun.run_id`를
`scan_id`로 바꾸어 부르지 않는다. C 공유 결과 repository와 B 정적 plan 연동,
실제 #78 ID chain, 물리적으로 다른 두 호스트 검증은 여전히 완료 게이트다.
카드 VDB 추가 범위는 #79 댓글 `issuecomment-6624420876`에 기록됐다.

## 목표

#78이 선택하는 실제 n8n-claw Memory Delete 사례 한 건을 공용 PostgreSQL에서 입력, A 분석, B retrieval/plan, C 결과의 계약상 ID로 조회한다. 두 번째 호스트에서 각 소유 저장소가 제공하는 결과와 #79 완료에 실제로 필요한 원본 바이트를 정확한 hash/size 기대값으로 확인한다.

두 호스트에서 workflow를 다시 실행하는 것이 아니라, 한 실제 producer 결과를 다른 호스트에서 같은 ID와 데이터 관계로 조회·복원하는 것을 #79 acceptance로 정의한다.

## 저장·공유 원칙

- PostgreSQL은 공용 조회 지점이다. 입력, A, B, C 데이터는 각 모듈 소유 repository와 migration으로 저장·조회한다.
- B의 plan/retrieval/KB snapshot BYTEA는 현재 저장소에 있는 그대로 사용한다. plan_id로 복원하며 S3로 이동하지 않는다.
- A의 graph.analysis BYTEA는 PR #73이 병합·검증된 뒤에만 실제 저장 경로로 취급한다. 병합 전 계획상의 제안이다.
- C result persistence와 결과 ID는 #51/#78이 확정한 계약을 따른다. PR #83의 adapter/catalog은 C 결과 공용 저장소와 같은 것으로 간주하지 않는다.
- workflow revision은 sources.json의 pinned upstream URL/commit/SHA/크기와 collect.py 경로를 사용한다. 각 호스트가 필요한 때 원본을 가져오고 신뢰된 기대값으로 검증한다. 원본 워크플로를 S3로 복제하지 않는다.
- 예시 카드/fixture는 Git에서 bootstrap/test data로 사용한다. 현재 B producer는 CLI의 --cards 로컬 디렉터리에서 ScenarioCard JSON을 읽고 KEYWORD 검색을 하지만, 목표 흐름에서는 새 plan의 카드 후보를 공유 PostgreSQL/pgvector VDB에서 조회한다. 이를 위해 B 소유 card catalog, content hash/provenance, embedding profile과 vector 저장·검색을 구현한다. 카드 identity는 (card_id, version)이며 CVE는 선택적 evidence metadata다. 동일 CVE를 참조하는 서로 다른 revision·환경·증거의 카드를 허용하고, CVE ID나 vector similarity를 카드 dedup key로 쓰지 않는다. 같은 identity에 hash가 달라지면 명시된 version/change 정책에 따라 거부하거나 새 버전으로 등록한다.
- 각 카드 벡터는 카드 내용이나 embedding profile이 바뀔 때 갱신한다. 서로 다른 input/A analysis revision마다 새 query embedding과 retrieval/plan을 만든다. 이미 저장된 plan을 다시 보는 소비 호스트는 새 검색을 하지 않고 plan_id의 PostgreSQL 결과를 읽는다.
- B는 선택된 card_id/version/score/profile과 제외 이유를 기록하고 실제 사용 카드 JSON을 kb-snapshot.json에 고정해 plan/retrieval과 함께 PostgreSQL BYTEA에 저장한다. 유사도는 후보 관련도를 나타낼 뿐 동일성·무결성·위험도 증명이 아니다. 정확한 content는 SHA-256으로 검증한다. CONTRACT_EXAMPLE 자료는 실제 사례 acceptance 증거가 아니다.
- B source_revision의 raw_path는 현재 FLOWDELTA_KB_RAW_ROOT의 로컬 파일을 가리킨다. 실제 사례가 그 파일을 두 번째 호스트에서 필요로 하는지 먼저 소비 계약으로 확인한다. metadata가 있다는 사실만으로 전체 raw root 또는 해당 byte를 공유 대상으로 만들지 않는다.
- C raw Semgrep report는 코드 줄을 포함할 수 있고 현재 experiments 문서가 var/ 로컬 보관 경계를 둔다. 공유 권한이 확인되지 않으면 업로드하지 않는다. 승인된 sanitized 결과는 별도의 artifact이며 raw bytes와 동일하다고 주장할 수 없다.
- 해시 검증 기대값은 pinned manifest 또는 모듈 owner의 durable DB metadata에서 가져온다. 다운로드한 데이터 자체에서 기대값을 산출하지 않는다.

## S3 결정

S3를 결정하기 전에 선정 사례의 제한된 local-only bytes를 PostgreSQL BYTEA로 저장할 수 있는지 먼저 검토한다. artifact ID로 DB에서 바이트와 SHA/크기를 읽을 수 있으면 S3 없이도 다른 호스트가 조회하고 검증할 수 있다. 저장 위치와 migration은 해당 데이터의 모듈 owner가 결정하며, 기존 B output 전용 row에 무관한 파일을 넣지 않는다.

파일 크기·건수, DB 용량, 백업/복구 요구나 팀 표준상 BYTEA가 부적합할 때 S3를 별도 설계/PR로 추가한다. 공유 권한, URI metadata owner, private bucket/prefix, 최소 권한 role, trusted expected SHA-256/size 확인, 부분 결과 미노출, lifecycle/복구 정책을 확정한다. Python 애플리케이션이 직접 객체를 읽고 쓸 때 boto3 SDK를 사용한다. 필요한 경우 S3 PR을 #79의 선행 의존성으로 표시한다.

현재 문서 PR #40의 artifacts.storage_uri는 참고 설계이며 실제 운영 schema나 적용 migration이라고 가정하지 않는다. 먼저 owner repository의 기존 ID/URI metadata가 요구를 충족하는지 확인한다. 별도 artifact table이나 B의 0004 migration을 미리 추가하지 않는다.

## 실제 사례 데이터 흐름

| 데이터 | 현재 확인된 출처 | 두 번째 호스트의 접근 경로 |
|---|---|---|
| pinned n8n workflow revision | experiments/n8n-claw-c/sources.json의 upstream URL, commit, SHA, size | 기존 collect.py로 재수집 및 기대 SHA/크기 확인 |
| ScenarioCard | 예시와 bootstrap은 Git; 목표 실행 catalog와 embedding은 B 소유 PostgreSQL/pgvector | 매 distinct analysis마다 VDB에서 후보 검색 후 새 retrieval/plan 생성. 과거 plan은 plan_id의 kb-snapshot으로 조회 |
| 입력/source ID와 source bytes | #48/#49 owner contract 필요. B raw file은 현재 local FLOWDELTA_KB_RAW_ROOT | 사례 필수 여부를 확정하고 owner가 제공하는 source fetch/repository 경로 사용 |
| A analysis | PR #73에서 제안한 graph.analysis BYTEA, 별도 graph migration | PR 병합 후 owner repository에서 exact analysis_id로 조회 |
| B retrieval/plan/KB snapshot | PostgreSQL BYTEA, 현재 B knowledge repository | exact plan_id 조회/기존 export 경로 |
| C result | #51/#78 owner contract와 persistence 상태 확인 필요 | 실제 결과 ID로 owner repository 조회 |
| C raw Semgrep report | 현재 로컬 var/에 제한, source lines 포함 가능 | 공유 권한이 확인된 경우에만 별도 승인 경로. 아니면 raw 교차 호스트 요구는 미해결 |

## 이슈·PR 의존성 처리

2026-11-03 확인 범위에서 #79/#78/#48/#49/#50/#51은 OPEN, PR #69는 MERGED, PR #58/#59/#73/#83은 OPEN + CHANGES_REQUESTED였다.

- #48/#49 소유자와 선정 case/input/source ID 및 원본 접근을 확정한다.
- #50과 공용 PG 접속, role, owner별 migration 순서를 확정한다.
- #51/#78과 C 결과의 실제 ID, persistence 및 실제 사례를 확인한다. run_id를 scan_id로 임의 대응하지 않는다.
- #58은 Memory/File repository 경로다. 공용 PostgreSQL persistence가 포함된 것으로 확대해석하지 않는다.
- #59는 pipeline runner다. #78 실제 producer 실행에 필요한 경우에만 해당 경로의 merge/대체 구현을 gate로 둔다.
- #73은 A graph repository 제안이다. 병합 후 기존 경로를 재사용하고 B migration graph에 합치지 않는다.
- #83은 C adapter/catalog 경로다. C result repository 완료 여부는 별도로 확인한다.
- OPEN 상태인 PR 전부를 일괄 blocker로 만들지 않는다. 실제 deliverable에서 필요한 기능만 해당 단계의 gate로 둔다.

## 구현 범위와 제외 범위

**포함:** 실제 사례와 필수 bytes inventory, 공용 PostgreSQL/pgvector 연결·role·migration, B 소유 ScenarioCard catalog와 embedding search, distinct analysis별 retrieval/plan 생성, owner repository exact-ID 조회, 두 호스트 검증, 운영 runbook.

**조건부:** S3 adapter/SDK, URI metadata, artifact migration은 기존 공유 경로로 충족하지 못하는 권한 확인된 필수 byte가 있을 때 별도 작업으로 추가한다.

**제외:** 모든 파일의 S3 복제, 원본 workflow/raw report의 무단 재배포, B BYTEA 이전, generic artifacts table 선제 도입, 서로 다른 module revision graph 병합, 운영 대량 NVD/OSV/GHSA ingestion, 임의 workflow 실행.

## Acceptance 기준

1. #78의 실제 Memory Delete 실행이 producer로 사용되고, 등록된 adapter/catalog 및 발행된 실제 ID가 기록된다.
2. 소비 호스트가 exact case ID와 각 owner contract ID로 같은 사례 lineage를 확인한다. 서로 다른 사례의 유효한 ID 조합은 실패한다.
3. 빈 DB 설치, owner가 지원하는 revision에서 upgrade, repeat migration, app role 최소 권한이 전용 PostgreSQL에서 검증된다.
4. 각 필수 byte의 출처와 신뢰 기대 SHA/size가 명시되고, 두 번째 호스트에서 실제 바이트와 비교된다. 기존 B plan의 사용 카드 집합은 저장된 kb-snapshot으로 확인한다. 서로 다른 analysis revision은 VDB 후보 검색을 거쳐 별도 retrieval/plan을 만든다. 같은 카드/profile은 재사용하고 카드 content/profile 변경 때만 다시 임베딩한다.
5. 실제 물리적으로 다른 두 호스트의 기록을 제출한다. 단일 호스트 두 role, unit fake, contract example은 이 기준을 대신하지 않는다.
6. S3가 필요하다고 결정된 경우에만 승인된 파일에 대한 두 호스트 object access와 mismatch/failure 처리를 검증한다.
