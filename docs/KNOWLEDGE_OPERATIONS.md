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

`flowdelta plan ... --persist`는 기존 파일 산출물을 만든 뒤 `plan.json`, `retrieval.json`,
`kb-snapshot.json`의 원본 bytes와 SHA-256을 PostgreSQL에 한 트랜잭션으로 저장한다. DB 실패 시
생성된 파일은 남으며 `flowdelta knowledge store-plan <plan.json>`으로 저장을 재시도할 수 있다.
같은 ID와 같은 bytes는 멱등 처리하고, 같은 ID에 다른 bytes를 저장하면 충돌로 거부한다.

```sh
uv run --frozen --extra knowledge flowdelta plan <analysis-bundle> \
  --catalog <template-catalog.json> --cards <cards-dir> --out <producer-dir>/plan.json --persist
uv run --frozen --extra knowledge flowdelta knowledge export-plan <plan-id> --out <new-b-output-dir>
```

`export-plan`은 정확한 `plan_id`를 조회해 세 파일의 원래 bytes를 새 디렉터리에 복원한다.
이 결과는 B 저장소 왕복을 위한 출력이며 전체 C bundle은 아니다. 기존 C 입력에 연결하려면
handoff, A의 analysis와 template catalog를 포함해 `flowdelta bundle`로 manifest를 만들어야 한다.

현재 B 산출물은 각 계약 파일의 8 MiB 상한 안에서 PostgreSQL `BYTEA`에 저장된다. 이 방식은
로컬 통합과 실제 bytes 왕복을 가능하게 하지만, docs PR #40의 공용 artifact URI 구조를 대체하지
않는다. 팀 공용 artifact 저장소와 C의 resolver가 정해지면 해당 API에 맞춰 옮긴다.

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
새 서버에 같은 PostgreSQL major·pgvector 버전을 준비하고 DB와 원본 파일을 복원한 뒤
URL·원본 경로를 변경한다. migration/status 및 원본 해시 검증 후 수집을 재개한다.
비밀번호는 새 서버에서 재발급한다. 자동 destructive downgrade는 제공하지 않는다.
