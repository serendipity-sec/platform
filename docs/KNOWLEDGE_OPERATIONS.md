# B 지식 저장소 운영

현재 구현: 로컬 PostgreSQL·pgvector, 명시적 migration, 원본 revision 저장.
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
임의로 수행하지 않는다.

원본 파일 쓰기와 DB 쓰기는 `storage_transaction()`으로 함께 관리한다. DB 커밋이 실패하거나
롤백되면 해당 트랜잭션에서 새로 만든 미참조 파일을 정리한다. 프로세스가 강제 종료되어
남은 고아 파일은 `cleanup-raw`가 24시간 이상 된 파일 중 DB revision에서 참조하지 않는 것만
제거한다. 실행 중인 저장 작업과 정리는 PostgreSQL advisory lock으로 조정한다.

## 검증

일반 단위 테스트는 서버를 요구하지 않는다. 실제 DB 검증은 전용 테스트 DB를 만들고
`FLOWDELTA_KB_TEST_DATABASE_URL`, `FLOWDELTA_KB_TEST_MIGRATION_URL`을 지정한 후 실행한다.
운영 DB URL을 테스트 변수에 복사하지 않는다.

## 서버 이전

수집을 중지하고 DB의 일관된 dump와 `FLOWDELTA_KB_RAW_ROOT`를 함께 보관한다.
새 서버에 같은 PostgreSQL major·pgvector 버전을 준비하고 DB와 원본 파일을 복원한 뒤
URL·원본 경로를 변경한다. migration/status 및 원본 해시 검증 후 수집을 재개한다.
비밀번호는 새 서버에서 재발급한다. 자동 destructive downgrade는 제공하지 않는다.
