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
남은 고아 파일은 `cleanup-raw`가 24시간 이상 된 파일 중 DB revision에서 참조하지 않는 것만
제거한다. 실행 중인 저장 작업과 정리는 PostgreSQL advisory lock으로 조정한다.

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

## 서버 이전

수집을 중지하고 DB의 일관된 dump와 `FLOWDELTA_KB_RAW_ROOT`를 함께 보관한다.
새 서버에 같은 PostgreSQL major·pgvector 버전을 준비하고 DB와 원본 파일을 복원한 뒤
URL·원본 경로를 변경한다. migration/status 및 원본 해시 검증 후 수집을 재개한다.
비밀번호는 새 서버에서 재발급한다. 자동 destructive downgrade는 제공하지 않는다.
