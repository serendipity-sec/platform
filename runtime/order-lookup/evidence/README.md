# 실제 정상 실행 근거

[local-2026-10-11.json](local-2026-10-11.json)은 Windows Docker Desktop의 Linux 컨테이너에서
실행한 자료다. before/after 비교 두 번, 총 정상 실행 네 번이 성공했고 각 실행의 자원 정리가 완료됐다.
**하나의 합성 사례를 반복한 자료이며 독립적인 업무 네 개를 평가한 결과가 아니다.**

- `baseline-005`: 정상 before/after 실행, 별도 상태·주문 데이터 보존·서비스 이벤트·정리 확인.
- `baseline-006`: 같은 비교를 새 상태에서 재현하고 실제 fixture 소스 해시·라이브러리 버전도 확인.
- 각 응답은 HTTP 200, 서비스 이벤트는 ORD-1001의 ORDER_READ 1건.
- 각 실행의 시작 이벤트는 0건, 초기/최종 주문 데이터 해시는 동일.
- n8n은 2.42.6, 내부 네트워크, 호스트 공개 포트 없음.
- 보안 outcome은 null. A/B/C의 실제 자동 연결 결과가 아니다.

`embedded_artifacts`는 실제 JSON 자료와 원래 바이트의 SHA-256/크기를 함께 담는다.
해시 대상은 UTF-8, 정렬된 키, indent 2, 마지막 LF가 있는 `flowdelta.ingestion.json_bytes(content)`다.
`record_without_artifact_list`는 실제 run.json에서 파일 목록만 뺀 값이며, 원래 run.json의 해시도 남긴다.
원본 명령 로그와 실행 폴더는 로컬 `var/baseline-005`, `var/baseline-006`에 보존한다.
자격 값·n8n DB·주문 DB와 private 폴더는 공유하지 않는다.

준비 중 실패한 시도 네 번도 요약했다. 실제 업무 요청 전 오류였으며 성공 실행 수에 넣지 않았다.
CI의 결과는 PR에서 별도로 확인한다. 이 파일을 CI 실행 근거로 대체 사용하지 않는다.
