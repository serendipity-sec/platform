# B가 실제 생성한 주문 조회 계획

`flowdelta.knowledge/1.0.0`이 `examples/contracts/order-lookup/`의 분석 예시와
카드·catalog를 읽어 생성했다. plan과 retrieval은 MODULE_OUTPUT이며,
입력 분석·카드는 CONTRACT_EXAMPLE을 유지한다. A 실제 분석이나 C 실행 자료는 아니다.

- `plan.json`: 정상 입력, 등록 템플릿, 초기 상태, 모든 obligation의 연결.
- `retrieval.json`: 결정적 검색 순위·점수·선택/제외 이유, KB 파일 참조.
- `kb-snapshot.json`: 검색에 사용한 카드와 정규 JSON 해시.

파일 배치·재현 명령은 [B 사용 안내](../../../docs/KNOWLEDGE.md)를 따른다.
이 디렉터리는 B 산출물만 담으므로 전체 check-pipeline bundle이 아니다.
팀장 작성기가 입력·분석·catalog와 묶을 때 세 파일을 bundle 루트에 배치한다.

실행 여부: NOT_RUN. 보안 결과는 판정하지 않았다.
