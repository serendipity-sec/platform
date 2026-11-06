# B 지식 저장소 운영

현재 구현: 로컬 PostgreSQL·pgvector, 명시적 migration, 입력 JSON 원본 바이트를 보존하는
revision 저장. `source_record.document`는 조회용 JSONB 표현이다.
전체 CVE 수집·색인·보안 실행 완료를 의미하지 않는다.

## 로컬 시작 (PowerShell)

```powershell
./tools/prepare-knowledge.ps1
docker compose --env-file var/knowledge/local.env -f runtime/knowledge/compose.yaml up -d --wait
uv sync --frozen --extra baseline --extra knowledge
Get-Content var/knowledge/local.env | ForEach-Object {
    $pair = $_.Split('=', 2)
    [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'Process')
}
uv run --frozen --extra knowledge flowdelta knowledge migrate
uv run --frozen --extra knowledge flowdelta knowledge status
uv run --frozen --extra knowledge flowdelta knowledge cleanup-raw
```

환경 파일을 출력하거나 커밋하지 않는다. 생성 도구는 기존 파일을 덮어쓰지 않는다.
DB는 localhost:55432에 바인딩하며 데이터는 `knowledge-postgres` named volume에 남는다.
일반 중지는 `docker compose ... stop`을 사용한다. `down -v`는 데이터를 삭제한다.

`FLOWDELTA_KB_MIGRATION_URL`은 DDL 계정, `FLOWDELTA_KB_DATABASE_URL`은 앱 계정이다.
앱 계정은 knowledge schema의 DML만 허용한다. 다른 서버에서는 관리자가 앱 역할을
준비하고 migration 실행 후 동일 권한을 부여한다. DB 생성·계정 생성은 migration이
임의로 수행하지 않는다. `FLOWDELTA_KB_RAW_ROOT`는 절대 경로를 사용하며, 기본값은 OS별
사용자 데이터 디렉터리다. 로컬 준비 스크립트는 저장소의 `var/knowledge/raw`를 절대 경로로
설정하므로 실행 작업 디렉터리가 달라져도 같은 원본 저장소를 사용한다.

원본 파일 쓰기와 DB 쓰기는 `storage_transaction()`으로 함께 관리한다. DB 커밋이 실패하거나
롤백되면 해당 트랜잭션에서 새로 만든 미참조 파일을 정리한다. 프로세스가 강제 종료되어
남은 고아 파일은 `cleanup-raw`가 24시간 이상 된 파일 중 DB revision이나 재처리 가능한
격리 행에서 참조하지 않는 것만 제거한다. 실행 중인 저장 작업과 정리는 PostgreSQL
advisory lock으로 조정한다.

## B plan 저장과 조회

로컬 카드 경로는 기존처럼 `--cards`로 `load_cards()`를 호출하고 KEYWORD 계획을 만든다.
`--persist`를 주면 파일을 먼저 만든 뒤 `plan.json`, `retrieval.json`, `kb-snapshot.json`의
원본 bytes와 SHA-256을 PostgreSQL에 한 트랜잭션으로 저장한다. DB 저장에 실패해도 생성한 파일은
남으므로 `flowdelta knowledge store-plan <plan.json>`으로 저장을 재시도할 수 있다.
같은 ID와 같은 bytes는 멱등 처리하고, 같은 ID에 다른 bytes를 저장하면 충돌로 거부한다.

```sh
uv run --frozen --extra knowledge flowdelta plan <analysis-bundle> \
  --catalog <template-catalog.json> --cards <cards-dir> --out <producer-dir>/plan.json --persist
uv run --frozen --extra knowledge flowdelta knowledge export-plan <plan-id> --out <new-b-output-dir>
```

공유 카드 모드는 미리 색인된 PostgreSQL/pgvector catalog에서 후보를 검색한다. 이 모드는 항상
정확한 B 출력을 PostgreSQL에 저장하며 `--persist`를 따로 요구하지 않는다. `--cards`와
`--shared-cards`는 함께 쓸 수 없다. 공유 검색이나 저장이 실패하면 keyword 경로로 자동 전환하지
않는다.

```sh
# 카드가 참조한 source artifact를 bundle root 기준으로 검증하고 shared catalog에 색인
uv run --frozen --extra knowledge flowdelta knowledge index-cards <analysis-bundle> \
  --cards <cards-dir>

# 새로운 analysis마다 QUERY 임베딩·벡터 검색으로 새 plan을 생성하고 B bytes를 저장
uv run --frozen --extra knowledge flowdelta plan <analysis-bundle> \
  --catalog <template-catalog.json> --shared-cards --out <producer-dir>/plan.json
```

등록된 `STATIC_SCAN` catalog도 같은 명령으로 소비한다. A의 영향 경로가 여러 개라면
`--path-id <impact-path-id>`로 선택하며, 선택 요청은 벡터 검색의 retrieval/plan ID에 포함된다.
다른 경로를 선택한 검색 결과를 재사용하려 하면 거부한다. 경로 미선택·잘림·미지원은 기존 정적
계획의 불확실성으로 보존하며 `security_outcome=null`을 유지한다.

`index-cards`는 bounded `load_cards()`로 카드를 읽고 bundle root의 source provenance를 확인한다.
현재 실행 중인 Ollama model digest에서 profile을 메타데이터만으로 해석한 뒤 색인 API에 전달한다.
색인 명령은 profile 확인을 위해 기존 카드를 다시 임베딩하지 않는다. 같은 `(card_id, version)`에
다른 canonical JSON이 들어오면 충돌로 거부한다. 내용을 바꾼 카드는 새 `version`으로 등록한다.
모델 digest나 query instruction이 바뀌면 새 profile로 색인한다. 이전 profile 벡터는 profile별
별도 공간에 남지만 기존 plan replay에는 쓰지 않는다. replay는 저장된 `kb-snapshot.json`과 B
output bytes를 사용한다. profile이 색인되지 않았거나 공유 DB에 접근할 수 없으면 오류가 나며
keyword 검색으로 숨겨서 대체하지 않는다.

최초 모델 pull 직후에는 `embedding-smoke`로 모델을 실행해 둘 수 있다. `index-cards`의
profile 확인은 현재 실행 중인 1024차원 모델을 요구하며, 차가운 모델은 `EMBEDDING_MODEL_NOT_FOUND`로
실패한다. embedding-smoke는 합성 입력을 보내 실제 모델을 준비하고 profile/vector 개수만
출력한다. 실제 카드 색인은 누락된 DOCUMENT 벡터만 embedding endpoint에 보낸다.

`export-plan`은 정확한 `plan_id`를 조회해 세 파일의 원래 bytes를 새 디렉터리에 복원한다.
재검색이나 임베딩을 반복하지 않는다. 이 결과는 B 저장소 왕복을 위한 출력이며 전체 C bundle은
아니다. 기존 C 입력에 연결하려면 handoff, A의 analysis와 template catalog를 포함해
`flowdelta bundle`로 manifest를 만들어야 한다.

현재 B 산출물은 각 계약 파일의 8 MiB 상한 안에서 PostgreSQL `BYTEA`에 저장된다. 이 경로는
현재 B repository의 exact `plan_id` 조회·복원에 사용한다. 이 데이터를 generic artifact store나
S3로 옮기는 작업은 구현되어 있지 않으며, 이번 경로가 그런 이전을 약속하지 않는다.

## 검증

일반 단위 테스트는 서버를 요구하지 않는다. 실제 DB 검증은 전용 테스트 DB를 만들고
`FLOWDELTA_KB_TEST_DATABASE_URL`, `FLOWDELTA_KB_TEST_MIGRATION_URL`을 지정한 후 실행한다.
운영 DB URL을 테스트 변수에 복사하지 않는다.

## 공통 수집 흐름

소스 adapter는 `SourceAdapter` 계약을 구현한다. `fetch_page(cursor, page_size)`는
원본 JSON bytes, 소스의 record ID, 선택적 수정 시각, 다음 opaque cursor와 완료 여부를
돌려주고, `normalize(document)`는 공통 저장 형태를 돌려준다. 원본 API 호출과
인증·재시도 규칙은 소스별 adapter가 담당한다.

현재는 공통 runner만 제공하며 NVD·OSV·GHSA adapter와 `flowdelta knowledge sync`
명령은 아직 없다. adapter가 준비되면 아래처럼 명시적으로 실행한다.

```python
from flowdelta.knowledge.database import engine_for
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.knowledge.source_ingestion import ingest_source

settings = KnowledgeSettings.from_environment()
engine = engine_for(settings)
try:
    report = ingest_source(
        engine,
        settings.raw_root,
        adapter,  # 소스별 SourceAdapter 구현체
        page_size=100,
        max_pages=100,
    )
finally:
    engine.dispose()
print(report)
```

한 페이지의 revision·정규 레코드·격리 오류와 다음 cursor는 같은 PostgreSQL
transaction에서 커밋된다. 페이지 저장이 실패하면 cursor도 이전 값으로 남는다.
같은 revision은 중복 저장하지 않는다. `max_pages`에 도달하기 전에 소스가 끝나지
않으면 보고서는 `PENDING`이고, 다시 실행하면 DB의 cursor부터 이어간다. 전체 페이지를
읽으면 `COMPLETE`다. 잘못된 record는 `knowledge.sync_failure`에 고정 오류 코드로
격리하고 뒤의 record 처리는 계속한다.

동일 소스의 수집은 provider 호출 전부터 실행 종료까지 PostgreSQL advisory lock으로
직렬화한다. 프로세스가 죽어 lock이 풀렸는데 상태가 `RUNNING`이면 다음 실행이 이를
`FAILED`와 `KNOWLEDGE_SOURCE_RUN_INTERRUPTED`로 바꾸고 cursor를 보존한 채 중단한다.
원인을 확인한 뒤 다시 실행하면 보존된 cursor부터 이어간다.

현재 공통 상한은 페이지당 500개, 실행당 1,000페이지, cursor 16 KiB, 원본 JSON 1 MiB,
정규 JSON 8 MiB, record ID 512자, JSON 깊이 64다. `knowledge.source_revision`은
받은 원본 bytes와 SHA-256을 보존하고, `knowledge.source_record.document`에는 별도
정규 JSON을 둔다.

격리 레코드를 재시도할 때 `modified_at`이 없으면 현재 문서가 없는 경우 저장할 수 있다.
현재 문서도 시각이 없는 같은 checksum의 재시도는 멱등하게 성공한다. 다른 checksum의
시각 없는 레코드는 기존 문서를 교체하지 않고 `KNOWLEDGE_SOURCE_RECORD_STALE`로 격리한다.
같은 ID에 서로 다른 시각 없는 격리본이 여럿 있으면 어떤 것도 저장하지 않고 모두
`KNOWLEDGE_SOURCE_REVISION_ORDER_UNKNOWN`으로 남긴다. 재시도 정렬 순서는 최신 여부를 뜻하지
않는다. 일반 수집의 도착 순서 처리와 시각이 있는 레코드의 비교 규칙은 유지한다.

수집 상태와 격리 건수는 다음 쿼리로 확인한다.

```sql
SELECT source, status, cursor, records_seen, error_code, updated_at
FROM knowledge.sync_state
ORDER BY source;

SELECT source, error_code, count(*) AS records
FROM knowledge.sync_failure
GROUP BY source, error_code
ORDER BY source, error_code;
```

cursor에는 이어받기에 필요한 값만 담는다. API key, 인증 헤더, 원본 record 전체는
넣지 않는다. source/API 오류는 `FAILED` 상태와 고정 코드로 표시되며 provider 응답
본문이나 예외 메시지는 상태 테이블에 저장하지 않는다.

격리된 원본 JSON이 1 MiB 이하이면 원본 bytes와 외부 ID를 보존하므로 adapter를 수정한
뒤 일부를 다시 처리할 수 있다. 오래된 migration 행, 외부 ID를 보존하지 못한 행, 1 MiB를
초과한 원본은 자동 재처리 대상이 아니다. 재처리는 소스별 lock 아래에서 실행하며 한 번에
최대 500건까지 제한한다.

```python
from flowdelta.knowledge.source_ingestion import retry_quarantined_records

report = retry_quarantined_records(
    engine,
    settings.raw_root,
    adapter,  # 수정된 SourceAdapter 구현체
    limit=100,
)
print(report)
```

재처리가 성공하면 해당 외부 ID의 이전 격리 행과 잘못된 ID 별칭 행도 제거한다. 실패한
행은 새 고정 오류 코드와 함께 남아 다음 점검이나 재시도에 사용할 수 있다.

## 서버 이전

수집을 중지하고 DB의 일관된 dump와 `FLOWDELTA_KB_RAW_ROOT`를 함께 보관한다.
새 서버에 같은 PostgreSQL major·pgvector 버전(공유 카드 검색은 `hnsw.iterative_scan`을
쓰므로 pgvector 0.8 이상, 기준은 0.8.1)을 준비하고 DB와 원본 파일을 복원한 뒤
URL·원본 경로를 변경한다. migration/status 및 원본 해시 검증 후 수집을 재개한다.
비밀번호는 새 서버에서 재발급한다. 자동 destructive downgrade는 제공하지 않는다.

## A/B 공유 DB와 migration

A와 B는 연결에 같은 `KnowledgeSettings`와 `FLOWDELTA_KB_DATABASE_URL` /
`FLOWDELTA_KB_MIGRATION_URL`을 재사용하지만 migration graph와 version table은 분리되어 있다.
DDL 계정은 migration 실행에만 쓰고, 앱 계정은 소유 테이블의 허용된 조회·추가에 쓴다.
앱 시작 시 migration이나 schema DDL을 자동 실행하지 않는다.

```sh
# 한 번 준비할 때 두 모듈 migration을 각각 명시적으로 실행
uv run --frozen --extra knowledge flowdelta knowledge migrate
uv run --frozen --extra knowledge flowdelta analysis-db migrate
uv run --frozen --extra knowledge flowdelta knowledge status
```

B는 `kb_0001`부터 `kb_0004`까지 `public.alembic_version`으로 관리한다. A는 별도
`graph_0001`과 `public.alembic_version_graph`를 쓴다. 둘 중 하나의 적용이 다른 쪽을 대신하지
않는다. 두 물리 호스트는 같은 승인된 DB와 동일한 최소 권한 앱 role grants를 사용하되,
호스트나 팀원별 계정과 credentials는 분리해 관리할 수 있다. 접속 문자열이나 비밀번호는 로그,
명령 예시, 저장소에 남기지 않는다.

A가 생성한 analysis는 정확한 ID로 저장·조회한다. `find`/latest 자동 선택은 운영 흐름에서
사용하지 않는다.

```sh
uv run --frozen --extra knowledge flowdelta analysis-db store <analysis.json>
uv run --frozen --extra knowledge flowdelta analysis-db get <analysis-id> --out <analysis-copy.json>
```

B plan은 analysis bundle 안의 `analysis.json`을 소비한다. `analysis-db get`을 쓴 경우에도
그 파일과 대응하는 handoff 및 template catalog를 manifest가 검증하는 bundle에 넣고 해당
bundle로 plan을 만든다. `plan`은 DB에서 analysis를 추측하거나 최신 ID로 바꾸지 않는다.

## 공유 DB 교차 호스트 바이트 경로

| 필요한 데이터 | 현재 소유 경로 | 두 번째 호스트 확인 |
|---|---|---|
| pinned n8n workflow revision | `experiments/n8n-claw-c/sources.json`와 기존 `collect.py`의 upstream URL, commit, SHA-256, 크기 | 고정 revision을 다시 수집하고 신뢰된 manifest 값과 bytes를 비교한다. 원본 전체를 DB나 S3에 복제하지 않는다. |
| ScenarioCard catalog | B의 canonical card rows와 profile별 pgvector catalog | card ID/version과 content SHA-256을 확인한다. 같은 ID/version의 다른 bytes는 등록 충돌이며 변경은 새 version으로 한다. |
| plan에 실제 사용된 카드 | `kb-snapshot.json` | B의 저장된 snapshot에서 읽는다. 현재 VDB 검색을 다시 실행하지 않는다. |
| B `plan.json`, `retrieval.json`, `kb-snapshot.json` | 기존 B repository의 PostgreSQL `BYTEA`, 정확한 `plan_id` | `knowledge export-plan <plan-id>`로 원래 bytes를 복원한다. 세 파일 외의 전체 C bundle을 복원하는 명령은 아니다. |
| A `analysis.json` | A 소유 `graph.analysis` PostgreSQL `BYTEA`, 정확한 `analysis_id` | `analysis-db get <analysis-id>`를 사용하고 case/snapshot ID와 hash를 대조한다. |
| C raw Semgrep output | 현재 제한된 로컬 경로 | 코드 줄이 포함될 수 있어 권한·owner contract가 정해지기 전에는 이동하지 않는다. C 공용 결과 repository와 실제 ID 경로가 준비되어야 교차 호스트 검증을 완료할 수 있다. |

`knowledge index-cards`는 profile 확인에 현재 실행 중인 Ollama model을 요구한다. cold model은
`EMBEDDING_MODEL_NOT_FOUND`로 실패하므로 먼저 `flowdelta embedding-smoke`를 실행해 model을
올리고 확인한다. 색인에서 `CARD_CATALOG_PROFILE_ALREADY_INDEXING`을 받으면 해당 profile의
다른 작업이 끝난 뒤 같은 명령을 재시도한다. card row는 provider 호출 전에 보존될 수 있다.
provider가 실패한 경우 같은 card/version/profile로 재시도하면 누락 벡터만 채운다.

공유 plan은 분석의 구조화된 before/after change 값을 설정된 embedding endpoint에 `QUERY`로
전달한다. credential 종류와 민감해 보이는 필드명은 마커로 바꾸지만 이 redaction은 heuristic이며
모든 비밀을 찾는 보장은 아니다. endpoint를 신뢰하고 분석 내용을 검토한 뒤 실행한다. CLI는
request body나 vector를 로그에 쓰지 않는다. QUERY가 128 KiB 상한을 넘으면 자르지 않고 실패한다.

이 경로는 현재 확인된 byte만 다룬다. B source-ingestion의 전체 `FLOWDELTA_KB_RAW_ROOT`나 C raw
출력을 일괄 공유한다고 해석하지 않는다. S3, boto3, generic artifact store는 구현되어 있지 않다.
필수 byte가 기존 Git/upstream/owner DB 경로로 전달되지 않고 파일 크기·운영·백업 요구가
PostgreSQL `BYTEA`에 맞지 않을 때만 별도 owner 결정과 권한 검토 후 S3를 설계한다.

## 두 물리 호스트 acceptance 기록

아래 절차는 실제 #78 case와 owner가 발행한 ID가 준비된 뒤 실행한다. 이 runbook 추가만으로
실제 case나 두 번째 물리 호스트 검증이 수행된 것은 아니다. contract example, mock, 한 호스트의
서로 다른 DB role은 acceptance 증거가 아니다.

1. 호스트 A에서 사용할 실 DB를 준비하고, 승인된 DDL 계정으로 B `knowledge migrate`와 A
   `analysis-db migrate`를 각각 실행한다. `knowledge status`와 각 migration version table을
   확인하고 앱 계정 접속을 확인한다.
2. 실제 #78 사례의 `sources.json` pinned revision을 `collect.py`로 가져온다. 기록된 commit,
   기대 SHA-256/크기와 수집 bytes를 비교한다. 실제 ScenarioCard를 검토하고 색인 준비를 마친 뒤,
   `embedding-smoke`를 먼저 실행해 사용 model이 준비됐는지 확인한다.
3. 호스트 A에서 실제 분석을 만들고 `analysis-db store`로 정확한 `analysis_id`를 저장한다.
   그 analysis, handoff, catalog를 포함한 검증 bundle로 `knowledge index-cards`를 실행한 뒤
   `plan --shared-cards`를 실행한다. `plan --shared-cards`는 query 검색 후 B 세 산출물을 항상
   저장한다. `plan_id`, `analysis_id`, before/after snapshot ID, 실제 card ID/version/hash,
   embedding profile ID를 산출물과 owner metadata에서 기록한다.
4. 호스트 B에서 exact `analysis_id`를 `analysis-db get`으로 가져오고 exact `plan_id`를
   `knowledge export-plan`으로 복원한다. 출력 파일 SHA-256/크기를 producer가 저장한 bytes 또는
   신뢰된 owner metadata와 비교한다. 저장된 plan을 다시 열 때는 새 embedding search를 하지 않는다.
5. C 결과는 C owner가 제공하는 exact-ID repository로 확인한다. `StaticScanRun.run_id`를
   `scan_id`라고 부르거나 다른 C 결과 ID로 대체하지 않는다. C repository/static B plan 연결,
   실제 #78 ID가 준비되지 않으면 전체 acceptance는 미완료로 남긴다.
6. 없는 analysis/plan ID, 서로 다른 사례에서 가져온 ID 조합, 같은 card ID/version에 다른
   content hash, 손상되거나 크기가 달라진 export byte를 넣어 각각 조회·색인이 실패하는지 기록한다.

복사해 사용할 기록 template:

| 항목 | 기록 |
|---|---|
| 실행 날짜 / operator |  |
| Host A physical identifier / OS / checkout commit |  |
| Host B physical identifier / OS / checkout commit |  |
| PostgreSQL service identifier / B revision / A revision |  |
| 실제 case ID / pinned workflow commit |  |
| workflow expected SHA-256 + size / observed SHA-256 + size |  |
| analysis ID / case ID / before snapshot ID / after snapshot ID / analysis SHA-256 |  |
| indexed card IDs + versions + content SHA-256 / embedding profile ID |  |
| plan ID / retrieval ID / three exported SHA-256 + sizes |  |
| C owner repository / exact result ID / `run_id` meaning |  |
| negative ID/hash cases and results |  |
| result: PASS / FAIL / BLOCKED / NOT RUN; evidence references |  |

이번 Task 3 작업에서 실제 DB, Ollama service, 실제 사례 또는 두 물리 호스트는 사용하지 않았다.
따라서 위 acceptance 상태는 별도 실행 전까지 `NOT RUN`이며 C owner contract와 실제 #78 ID는
해결되지 않은 gate다.
