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

## FlowDelta 입력 모듈 — 2026-10-11

도입 전에 아래 공식 LICENSE 원문을 확인했다. 소스를 복사하거나 vendor하지 않고,
고정된 배포 패키지를 설치한다. 배포 시 각 배포물의 저작권·허가문을 보존한다.
전이 의존성 버전·해시는 `uv.lock`에 기록한다. 공개 배포 시 전이 의존성 고지도 함께 수집한다.

| 구성요소 | 용도 | 고정 버전 | 직접 확인한 LICENSE | 적용 사항 |
|---|---|---|---|---|
| Pydantic | 입력 계약·JSON Schema | 2.14.0 | [MIT](https://github.com/pydantic/pydantic/blob/v2.14.0/LICENSE) | 고지 보존 |
| Typer | CLI | 0.27.3 | [MIT](https://github.com/fastapi/typer/blob/0.27.3/LICENSE) | 고지 보존 |
| Rich | CLI 표시 | 15.0.0 | [MIT](https://github.com/Textualize/rich/blob/v15.0.0/LICENSE) | 고지 보존 |
| pytest | 단위·계약 테스트 | 9.1.1 | [MIT](https://github.com/pytest-dev/pytest/blob/9.1.1/LICENSE) | 개발 도구; 고지 보존 |
| Ruff | 형식·정적 검사 | 0.17.0 | [MIT 및 포함 코드 고지](https://github.com/astral-sh/ruff/blob/0.17.0/LICENSE) | 개발 도구; 포함 고지 보존 |
| mypy | 타입 검사 | 2.4.0 | [MIT 및 일부 PSF-2.0](https://github.com/python/mypy/blob/v2.4.0/LICENSE) | 개발 도구; 포함 고지 보존 |
| Hatchling | Python 패키지 빌드 | 1.32.4 | [MIT](https://github.com/pypa/hatch/blob/hatchling-v1.32.4/LICENSE.txt) | 빌드 도구; 고지 보존 |
| uv | 의존성 잠금·설치 | 0.9.30 | [MIT 선택](https://github.com/astral-sh/uv/blob/0.9.30/LICENSE-MIT) | 개발 도구; 고지 보존 |
| actions/checkout | CI 소스 체크아웃 | 11d5960a326750d5838078e36cf38b85af677262 (v4) | [MIT](https://github.com/actions/checkout/blob/11d5960a326750d5838078e36cf38b85af677262/LICENSE) | CI에서 원본 사용 |
| actions/setup-python | CI Python 설치 | ece7cb06caefa5fff74198d8649806c4678c61a1 (v6) | [MIT](https://github.com/actions/setup-python/blob/ece7cb06caefa5fff74198d8649806c4678c61a1/LICENSE) | CI에서 원본 사용 |

`examples/order-lookup`은 이 구현을 위해 작성한 합성 자료다. 외부 workflow나 운영 자료를
복사하지 않았다. n8n 자체를 Python 배포물에 포함하지 않으며, 기준 환경 명령은 별도의
공식 Docker 이미지를 실행한다.

## 팀장 기준 환경 — 2026-10-11

도입 전 공식 원문을 확인했다. 아래 항목은 팀 관리 정상 사례의 로컬 개발 환경이다.

| 구성요소 | 고정 버전 | 출처·라이선스 원문 | 사용 방식 |
|---|---|---|---|
| n8n Community image | 2.42.6; digest는 runtime lock에 기록 | [Sustainable Use License](https://github.com/n8n-io/n8n/blob/n8n%402.42.6/LICENSE.md) | 공식 이미지의 비상업적 로컬 사용. 엔터프라이즈 기능·재호스팅 판매 없음. n8n 바이너리·소스는 저장소에 포함하지 않음 |
| FastAPI | 0.141.1 | [MIT](https://github.com/fastapi/fastapi/blob/0.141.1/LICENSE) | 합성 주문 API. 배포물의 고지 보존 |
| Uvicorn | 0.53.0 | [BSD-3-Clause](https://github.com/Kludex/uvicorn/blob/0.53.0/LICENSE.md) | 합성 서비스의 ASGI 실행. 배포물의 고지 보존 |
| CPython / official Python image | 3.13.15-slim; digest는 runtime lock에 기록 | [PSF-2.0 및 포함 고지](https://github.com/python/cpython/blob/v3.13.15/LICENSE), [image build MIT](https://github.com/docker-library/python/blob/688a0b86bb44289df16a363e9f41d90514c1a5f9/LICENSE) | 공식 이미지를 로컬 서비스 기반으로 사용. 원본·OS 패키지 고지 보존 |
| n8n template 1750 | 확인일 2026-10-11 | [Creating an API endpoint](https://n8n.io/workflows/1750-creating-an-api-endpoint/) | 공개 설명의 API 구성 방식 참고. template JSON·코드 복사/재배포 없음 |
| n8n template 18823 | 확인일 2026-10-11 | [Order status workflow](https://n8n.io/workflows/18823-track-secure-order-status-and-email-alerts-with-webhooks-gmail-and-data-tables/) | 주문 조회 업무 조건 비교. JSON·코드 복사/재배포 없음 |

공개 template의 무료 사용 표기를 별도 코드·데이터 재배포 허가로 해석하지 않는다.
선택한 API 구성 방식에 맞춰 정상 주문 조회 workflow를 팀이 작성한다. 원본 template를 그대로
실행했다거나 운영 데이터를 확보했다고 표시하지 않는다. 공식 template API의 원본 JSON 요청은
403으로 실패했으며, 공개 설명만 확인했다. 상세 선택 근거와 대체 범위는 기준 환경 문서에 기록한다.

## 공개 입력 원본 — 2026-10-11

아래 LICENSE 원문을 고정 커밋에서 먼저 확인한 뒤 선택 파일을 수집한다. 외부 workflow는
정적 구조 검토에만 사용한다. 공개 원본 확보와 운영 중인 서비스의 사용 증거 확보는 별개다.

| 구성요소 | 고정 커밋 | 출처·라이선스 | 처리 |
|---|---|---|---|
| ibam28/customer-support-ai-automation-n8n | `71fee7067364e9ac139a55bca3683c15b5ec2c16` | [원본](https://github.com/ibam28/customer-support-ai-automation-n8n/tree/71fee7067364e9ac139a55bca3683c15b5ec2c16), [MIT LICENSE](https://github.com/ibam28/customer-support-ai-automation-n8n/blob/71fee7067364e9ac139a55bca3683c15b5ec2c16/LICENSE) | 선택한 workflow JSON·정상 입력 예시·업무 설명을 무수정 보존. Copyright (c) 2026 Bambang Saputra Jaya와 허가문을 함께 배포. 파일별 SHA-256·획득 URL 기록. 실행·배포 실적은 독립 확인하지 않음 |

## 우리 코드의 공개 라이선스 결정 상태

⬜ 미정. 재사용할 엔진들의 라이선스를 확인한 뒤 결정합니다.
(Apache-2.0 계열 엔진을 링크할 경우 Apache-2.0이 무난하고, 특허 조항이 있어 팀에도 유리합니다.)
