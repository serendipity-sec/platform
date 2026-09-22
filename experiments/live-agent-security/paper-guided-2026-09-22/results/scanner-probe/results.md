# 새 변형 유출 응답에 대한 정적 MCP 스캐너 probe

고정된 Cisco MCP Scanner 4.8.4의 YARA 정적 resources 모드에 정상·악성 응답 한 쌍을 넣었다. 두 fixture 결과는 탐지율 추정이 아니다.

| 사례 | 종류 | exit | 파싱 | 검사 항목 | unsafe | 판정 |
|---|---|---:|---:|---:|---:|---|
| T00-normal-response | benign | 0 | True | 1 | 0 | `NOT_DETECTED` |
| T01-transformed-exfil-response | malicious | 0 | True | 1 | 0 | `NOT_DETECTED` |
