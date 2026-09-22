# 재사용한 외부 구성요소

과목 규정: **오픈소스 라이선스 미준수 = 50% 감점**, 출처 미표기 복제 = **0점**.
외부 코드·규칙셋·데이터셋·이미지를 가져오기 **전에** 이 표에 먼저 적습니다.

| 구성요소 | 용도 | 버전/커밋 | 라이선스 | 출처 | 의무사항 | 확인자/날짜 |
|---|---|---|---|---|---|---|
| (예시) Cisco Skill Scanner | 스킬 정적 분석 엔진 후보 | ⬜ | ⬜ 확인 필요 | https://github.com/cisco-ai-defense/skill-scanner | ⬜ | ⬜ |
| (예시) Cisco MCP Scanner | MCP 메타데이터 검사 후보 | ⬜ | ⬜ 확인 필요 | https://github.com/cisco-ai-defense/mcp-scanner | ⬜ | ⬜ |

## 기록 규칙

- **버전/커밋**: "latest"는 안 됩니다. 태그나 커밋 해시를 고정합니다. 비교 실험의 재현성에도 필요합니다.
- **라이선스**: 저장소 루트의 LICENSE 파일을 직접 열어 확인합니다. README의 배지만 보고 적지 않습니다.
- **의무사항**: 저작권 고지 유지, NOTICE 포함, 변경 사항 명시, 소스 공개(카피레프트) 등 실제로 해야 할 일을 적습니다.
- GPL/AGPL 계열을 링크·수정할 경우 우리 저장소의 공개 의무가 생길 수 있습니다. 도입 전에 팀 전체가 확인합니다.
- 평가 데이터셋으로 공개 저장소의 파일을 쓸 때도 **재배포 가능 여부**를 별도로 확인합니다. 공개 열람 가능과 재배포 허용은 다릅니다.

## 후보 1 로컬 실험 — 2026-09-20

실험 전 공식 LICENSE 원문을 확인했습니다. 도구·규칙은 비교 실행용이며 서비스에 통합한 것이 아닙니다. 캐시/벤더 파일은 커밋 제외합니다. 실제 다운로드·실행 성공 여부와 고정 해시는 실험 보고서 및 evidence에 별도로 기록합니다.

| 구성요소 | 용도 | 고정 버전/커밋 | 라이선스·원문 | 처리 |
|---|---|---|---|---|
| OWASP Juice Shop | 로컬 대상 | v20.2.0 | [MIT](https://github.com/juice-shop/juice-shop/blob/v20.2.0/LICENSE) | 고지 유지, 원본 서비스 스캔 금지 |
| OWASP ZAP | 로컬 DAST | 2.17.0 | [Apache-2.0](https://github.com/zaproxy/zaproxy/blob/v2.17.0/LICENSE) | 고지 유지, 도구만 실행 |
| Shannon | 비교 도구 | 3.2.0 / 25b90b0611f15ab945e051dfee8ae78600d3a002 | [AGPL-3.0](https://github.com/KeygraphHQ/shannon/blob/25b90b0611f15ab945e051dfee8ae78600d3a002/LICENSE) | 무수정 독립 도구 평가; 서비스 통합 결정 없음 |
| Semgrep CE | 정적 분석 | 1.177.0 | [LGPL-2.1](https://github.com/semgrep/semgrep/blob/v1.177.0/LICENSE) | 무수정 도구 실행 |
| Semgrep community rules | 내부 비교 | 40b8c63f75dc7c22c8a77482d73bfb864b146f7e | [Rules License v1.0](https://semgrep.dev/legal/rules-license/) | 내부 평가만. 규칙 재배포·서비스 제공 허용 아님. 벤더 폴더 커밋 금지 |
| Ship Safe | 바이브코딩 특화 비교 | c604379e516d8db849dab4494cb24b10d6990a92 | [MIT](https://github.com/asamassekou10/ship-safe/blob/c604379e516d8db849dab4494cb24b10d6990a92/LICENSE) | 고지 유지 |
| FastAPI | C 앱 실행 | 0.141.1 | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) | 라이브러리 배포물의 고지 유지 |
| Uvicorn | C 로컬 HTTP 실행 | 0.53.0 | [BSD-3-Clause](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) | 라이브러리 배포물의 고지 유지 |
| SecureVibeBench | B 평가자료 확보 | HF 고정 SHA는 evidence/dataset-download.json 참조 | [MIT 원문](https://github.com/iCSawyer/SecureVibeBench/blob/main/LICENSE) | 저작권·허가문 보존. 원 저장소별 라이선스는 별도 확인; 참조 앱 다운로드·실행 안 함 |
| SPDF replication | B 결과 라벨 확보 | Zenodo 20761891 | [CC-BY-4.0 선언](https://zenodo.org/records/20761891) | 저자/DOI 명시. 원천 코드 재배포 권리 별도 확인; 코드 실행 안 함 |

연구 논문 PDF/HTML은 원문 확인용 로컬 캐시로 보관합니다. arXiv 열람 라이선스를 데이터셋 재배포 허가로 간주하지 않습니다. EmpricalVDR는 루트 라이선스가 확인되지 않아 코드 도입을 보류하고 메타데이터만 기록합니다.

## Skill·MCP 행동 및 검사기 비교 실험 — 2026-09-21~22

고정한 Cisco Skill Scanner 2.1.0, Cisco MCP Scanner 4.8.4, NVIDIA SkillSpector 2.11.2, AgentShield 1.6.0의 출처·라이선스·실행 모드와 미실행 Snyk snapshot은 [전용 재사용 기록](experiments/live-agent-security/scanner_setup/THIRD_PARTY.md)에 있습니다. 공식 LICENSE·존재하는 NOTICE·태그 commit 및 내려받은 파일의 SHA-256을 함께 보존했습니다. 설치된 프로그램·의존성·모델 가중치는 포함하지 않으며, 직접 작성한 축소 입력과 실행 결과를 공유합니다. 논문은 출처를 링크하고 원문 PDF를 재배포하지 않습니다.

## 우리 저장소의 라이선스

⬜ 미정. 재사용할 엔진들의 라이선스를 확인한 뒤 결정합니다.
(Apache-2.0 계열 엔진을 링크할 경우 Apache-2.0이 무난하고, 특허 조항이 있어 팀에도 유리합니다.)
