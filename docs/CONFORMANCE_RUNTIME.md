# 정상 노드 비교 실행의 게시 준비 조건

`flowdelta conformance`는 해시로 등록한 정상 fixture만 네트워크가 없는 n8n 2.42.6에서
실행한다. HTTP readiness는 서버 준비만 나타내므로, 바로 서버를 정지한 뒤 CLI 실행을
시작하면 하위 workflow의 게시 처리가 끝나지 않을 수 있다.

고정 엔진 소스에서 [서버 준비 신호](https://github.com/n8n-io/n8n/blob/34ab9c7bf3d0c6d2ec3422e12e0b970aea5717c7/packages/cli/src/server.ts#L123)가
먼저 열리고 [게시 처리](https://github.com/n8n-io/n8n/blob/34ab9c7bf3d0c6d2ec3422e12e0b970aea5717c7/packages/cli/src/commands/start.ts#L422)가
뒤이어 비동기로 진행된다. [하위 workflow 실행](https://github.com/n8n-io/n8n/blob/34ab9c7bf3d0c6d2ec3422e12e0b970aea5717c7/packages/cli/src/workflow-execute-additional-data.ts#L202)은
`workflow_published_version`의 매핑이 없으면 `Workflow is not active and cannot be executed.`로
실패한다. `activeVersionId`만 존재하는 것으로는 충분하지 않다.

따라서 서버가 준비된 뒤, 등록한 parent와 dependency 각각에 대해 읽기 전용 SQLite
조회로 다음을 최대 60초 동안 확인한다.

- `workflow_entity.activeVersionId`가 비어 있지 않다.
- `workflow_published_version.publishedVersionId`가 위 버전과 같다.
- 같은 workflow ID와 버전의 `workflow_history`가 존재한다.

완료하면 서버를 정지하고 한 번 실행한다. 완료되지 않으면
`CONFORMANCE_PUBLICATION_TIMEOUT`으로 실패한다. workflow 실행을 자동 재시도하지 않는다.
`publication.json`에는 workflow ID, 버전 ID, history 존재 여부, 준비 여부만 남긴다.

CI는 선택한 run/comparison/runtime-executions/publication JSON과 import/publish/bootstrap/
stdout/stderr 로그를 7일간 보존한다. 등록된 합성 fixture에 한정하며 private 디렉터리,
SQLite DB, 자격 파일은 업로드하지 않는다. 노드 출력 비교와 보안 판정은 별개이며,
이 검사의 보안 outcome은 null이다.
