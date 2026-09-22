# 검사 완료 상태 fail-closed 계약 실행 결과

- 생성 시각(UTC): `2026-09-21T20:07:24.730578+00:00`
- 계약: `scan-completion-fail-closed 1.0.0`
- 검증: **11/11 기대 판정 일치**
- `ALLOW`만 활성화를 허용한다. `DENY`와 `HOLD`는 모두 활성화를 차단한다.

## 판정 의미

| 판정 | 의미 | 활성화 |
|---|---|---|
| ALLOW | 완료·파싱 가능·원본 참조 존재·입력 SHA-256 일치·분석 범위 완전·위험 신호 없음 | 허용 |
| DENY | 신뢰할 수 있는 검사 결과에서 위험 신호 확인 | 차단 |
| HOLD | timeout, 출력 손상, 참조/다이제스트 문제, 분석 불완전, 미지원 입력 등으로 승인 근거 부족 | 차단 |

## 계약 사례

| 사례 | 자료 | 스캐너 | 기대 | 실제 | 이유 | 일치 |
|---|---|---|---|---|---|---|
| REAL_CISCO_MCP_M00_CLEAN | REAL | Cisco MCP Scanner 4.8.4 / YARA | ALLOW | ALLOW | COMPLETE_CLEAN_BOUND_SCAN | PASS |
| REAL_CISCO_MCP_M01_DETECTED | REAL | Cisco MCP Scanner 4.8.4 / YARA | DENY | DENY | CONFIRMED_SECURITY_FINDING | PASS |
| REAL_SKILLSPECTOR_DEFAULT_PARTIAL | REAL | NVIDIA SkillSpector 2.11.2 / --no-llm | HOLD | HOLD | INCOMPLETE_ANALYSIS, UNRESOLVED_REFERENCE | PASS |
| REAL_SKILLSPECTOR_STRICT_PARTIAL | REAL | NVIDIA SkillSpector 2.11.2 / --no-llm --fail-on-incomplete | HOLD | HOLD | INCOMPLETE_ANALYSIS, UNRESOLVED_REFERENCE, NONZERO_EXIT | PASS |
| REAL_AGENTSHIELD_ZERO_FILES | REAL | AgentShield 1.6.0 | HOLD | HOLD | ZERO_FILES_SCANNED, NO_ADAPTER_MATCH | PASS |
| SYNTHETIC_TIMEOUT | SYNTHETIC | synthetic Cisco-compatible contract fixture | HOLD | HOLD | SCAN_TIMEOUT | PASS |
| SYNTHETIC_MALFORMED | SYNTHETIC | synthetic Cisco-compatible contract fixture | HOLD | HOLD | MALFORMED_REPORT | PASS |
| SYNTHETIC_TRUNCATED | SYNTHETIC | synthetic Cisco-compatible contract fixture | HOLD | HOLD | TRUNCATED_REPORT | PASS |
| SYNTHETIC_MISSING_REF | SYNTHETIC | synthetic Cisco-compatible contract fixture | HOLD | HOLD | MISSING_REPORT_REFERENCE | PASS |
| SYNTHETIC_STALE_DIGEST | SYNTHETIC | synthetic Cisco-compatible contract fixture | HOLD | HOLD | STALE_INPUT_DIGEST | PASS |
| SYNTHETIC_CLEAN | SYNTHETIC | synthetic Cisco-compatible contract fixture | ALLOW | ALLOW | COMPLETE_CLEAN_BOUND_SCAN | PASS |

## 실제 저장 산출물에서 확인한 핵심

- Cisco MCP M00: 5개 항목이 모두 completed/SAFE이고 현재 입력 SHA-256이 검사 시점과 같아 `ALLOW`.
- Cisco MCP M01: `PROMPT INJECTION` HIGH 신호가 있어 `DENY`.
- SkillSpector 기본/strict: 위험 점수는 0이지만 `is_complete=false`, `status=partial`, 미해결 참조가 있어 모두 `HOLD`. strict의 종료 코드 1도 함께 기록된다.
- AgentShield: A/100이지만 `filesScanned=0`, adapter match 0이므로 `HOLD`.

## 원본 증거 매니페스트

경로와 SHA-256은 이 계약 실행이 실제로 읽은 바이트를 나타낸다. 기존 scanner 실행 파일에는 출력 파일의 사전 커밋 다이제스트가 없으므로, 아래 값은 이번 계약 실행에서 만든 증거 스냅샷이다.

| 사례 | 역할 | 원본 경로 | SHA-256 | bytes |
|---|---|---|---|---:|
| REAL_CISCO_MCP_M00_CLEAN | execution | `scanner_setup/runs/cisco-mcp/M00/execution.json` | `fe05d58cd1618c34748a7f22bfc5f48be4fe0eac2df1f69e68f3a100eeaf6a29` | 801 |
| REAL_CISCO_MCP_M00_CLEAN | scanner_report | `scanner_setup/runs/cisco-mcp/M00/stdout.txt` | `d80402b7edb19130944dc60bd1e3ec488ba22e1b24b7d8005fc13b6d3f24460f` | 2257 |
| REAL_CISCO_MCP_M00_CLEAN | scanner_input | `fixtures/mcp/normal.json` | `55e186a75108d5fb9d620ab96dbe493ab6285cdb43d7eb5580511b2c7eb0e6af` | 2015 |
| REAL_CISCO_MCP_M01_DETECTED | execution | `scanner_setup/runs/cisco-mcp/M01/execution.json` | `2db68ff475b915abe425d6387e666eb5b9d369e7af0d7dbd5cd488ab4270d91f` | 804 |
| REAL_CISCO_MCP_M01_DETECTED | scanner_report | `scanner_setup/runs/cisco-mcp/M01/stdout.txt` | `7e76a264a2595dd541e318edba08dbb126f398abc8374fbf0631e3d9e4090a5c` | 3242 |
| REAL_CISCO_MCP_M01_DETECTED | scanner_input | `fixtures/mcp/poisoned.json` | `946b82a26bf01ed8ab3867a5e7ab3f01aaec2e105ea95f66890b75b39d46ccbc` | 2249 |
| REAL_SKILLSPECTOR_DEFAULT_PARTIAL | execution | `scanner_setup/runs/skillspector/S00/execution.json` | `cfe750699e975c5d8bc22ac390b80cd96236b2e444ac4399b1dd38444129531c` | 974 |
| REAL_SKILLSPECTOR_DEFAULT_PARTIAL | scanner_report | `scanner_setup/runs/skillspector/S00/report.json` | `f573d1f82a0367e3ba506931155202dd4ef99b5e09bbe9bfb5066998061b1142` | 9236 |
| REAL_SKILLSPECTOR_DEFAULT_PARTIAL | scanner_input | `fixtures/skills/S00/project-summary/SKILL.md` | `31a42642a351e569a000420911780469d148a648a00e18cbb3fb13a4f76654bc` | 217 |
| REAL_SKILLSPECTOR_STRICT_PARTIAL | execution | `scanner_setup/runs/skillspector-strict/S00/execution.json` | `83f5d74651d4bfc8f9051322426654f03b01d9618dd104d0571dc77f9621b2c9` | 1016 |
| REAL_SKILLSPECTOR_STRICT_PARTIAL | scanner_report | `scanner_setup/runs/skillspector-strict/S00/report.json` | `829c031705a14cdde3700c8778199a0fd54907282b414abcf511d9871ab087a3` | 9236 |
| REAL_SKILLSPECTOR_STRICT_PARTIAL | scanner_input | `fixtures/skills/S00/project-summary/SKILL.md` | `31a42642a351e569a000420911780469d148a648a00e18cbb3fb13a4f76654bc` | 217 |
| REAL_AGENTSHIELD_ZERO_FILES | execution | `scanner_setup/runs/agentshield/S00/execution.json` | `cfbdf3ee96c39f3f33567e09d35f6806d8d4634479737f6aac9895050aa86e8f` | 868 |
| REAL_AGENTSHIELD_ZERO_FILES | scanner_report | `scanner_setup/runs/agentshield/S00/stdout.txt` | `b902234c2737563fdba6f1664a22e5638e3ceebab9ce5ccdd4f0a8a36c8750eb` | 9881 |
| REAL_AGENTSHIELD_ZERO_FILES | scanner_input | `fixtures/skills/S00/project-summary/SKILL.md` | `31a42642a351e569a000420911780469d148a648a00e18cbb3fb13a4f76654bc` | 217 |
| SYNTHETIC_TIMEOUT | synthetic_case | `synthetic://SYNTHETIC_TIMEOUT/case.json` | `c4ce37a4a893f0cd502c2512758dd5d144a9ef54c3e8b25b89a281fee3547cbd` | 593 |
| SYNTHETIC_TIMEOUT | synthetic_report | `synthetic://SYNTHETIC_TIMEOUT/report.json` | `70d00ece138bfdb24c2364c7694303c33ef00dbb82fc5156f951b5b17c9bc8f5` | 208 |
| SYNTHETIC_MALFORMED | synthetic_case | `synthetic://SYNTHETIC_MALFORMED/case.json` | `046615920eceebf17dd379695c2257a0ce82e068a2ab885864a0af581135390a` | 599 |
| SYNTHETIC_MALFORMED | synthetic_report | `synthetic://SYNTHETIC_MALFORMED/report.json` | `e7a7df512baaf25caac8d00245b2141da2549f219e39c839ea0cc2fc8af5ab2f` | 18 |
| SYNTHETIC_TRUNCATED | synthetic_case | `synthetic://SYNTHETIC_TRUNCATED/case.json` | `670a63d2a7482ca810deabbdee1d01121b42070698cd6aef95a91f58061d9fdc` | 598 |
| SYNTHETIC_TRUNCATED | synthetic_report | `synthetic://SYNTHETIC_TRUNCATED/report.json` | `12555079da5a05f1aa87490be4f903b7a70d1c1d585b9400618c66a6a590b69a` | 197 |
| SYNTHETIC_MISSING_REF | synthetic_case | `synthetic://SYNTHETIC_MISSING_REF/case.json` | `e2af1da608744d29767fe828f22e286d1361aed781d7bd5ee18a07af3745f4b7` | 560 |
| SYNTHETIC_MISSING_REF | synthetic_report | `synthetic://SYNTHETIC_MISSING_REF/report.json` | `70d00ece138bfdb24c2364c7694303c33ef00dbb82fc5156f951b5b17c9bc8f5` | 208 |
| SYNTHETIC_STALE_DIGEST | synthetic_case | `synthetic://SYNTHETIC_STALE_DIGEST/case.json` | `f79e5c681748dce017da97e0c942b5886012729f8c06dccd1acfbca71dec002a` | 608 |
| SYNTHETIC_STALE_DIGEST | synthetic_report | `synthetic://SYNTHETIC_STALE_DIGEST/report.json` | `70d00ece138bfdb24c2364c7694303c33ef00dbb82fc5156f951b5b17c9bc8f5` | 208 |
| SYNTHETIC_CLEAN | synthetic_case | `synthetic://SYNTHETIC_CLEAN/case.json` | `e2d0c492d8cd4cb8c5726ac26fec77b57275bf10b24be279bd7ccaafacd65b51` | 587 |
| SYNTHETIC_CLEAN | synthetic_report | `synthetic://SYNTHETIC_CLEAN/report.json` | `70d00ece138bfdb24c2364c7694303c33ef00dbb82fc5156f951b5b17c9bc8f5` | 208 |

## 구현 무결성

- `scan_gate_policy.py`: `17b79f547511d0842c0a2fc56c8e343a8cb2236efe1d216d36f8e01c1f3f41c7` (12148 bytes)
- `run_scan_gate_contract.py`: `760f5e0058028c12b996ff24da7b18b3b1154015384a07b00b9b9f71b96933f0` (15806 bytes)

이 결과는 작은 계약 사례 집합의 정책 검증이다. 전체 생태계의 탐지율이나 안전성을 뜻하지 않는다.
