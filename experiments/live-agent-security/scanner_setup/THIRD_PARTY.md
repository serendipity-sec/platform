# 실험 검사기 재사용 사전 기록

2026-09-21. 제품 코드에 편입하지 않고 전용 `.runtime/live-agent-security-scanners/` 환경에서 비교 검사한다. 공식 배포판을 고정하고 원본 LICENSE와 존재하는 NOTICE를 보관한다. 설치된 전이 의존성은 freeze/lock 파일에 기록한다.

| 도구 | 고정 배포판 | 라이선스 | 출처 | 재배포 시 의무 |
|---|---|---|---|---|
| Cisco Skill Scanner | 2.1.0 | Apache-2.0 | https://github.com/cisco-ai-defense/skill-scanner/releases/tag/2.1.0 | 라이선스, 저작권, 배포에 포함된 NOTICE 유지; 변경 파일 표시 |
| Cisco MCP Scanner | 4.8.4 | Apache-2.0 | https://github.com/cisco-ai-defense/mcp-scanner/releases/tag/4.8.4 | 라이선스, 저작권, 배포에 포함된 NOTICE 유지; 변경 파일 표시 |
| NVIDIA SkillSpector | 2.11.2 | Apache-2.0 | https://github.com/NVIDIA/SkillSpector/releases/tag/v2.11.2 | 라이선스, 저작권, 배포에 포함된 NOTICE 유지; 변경 파일 표시 |
| AgentShield | 1.6.0 | MIT | https://github.com/affaan-m/agentshield/releases/tag/v1.6.0 | 저작권 및 허가 문구 유지 |
| Snyk Agent Scan | 0.6.3, 설치하지 않음 | Apache-2.0 | https://github.com/snyk/agent-scan/releases/tag/v0.6.3 | 이 작업에서는 문서 조사만 수행 |

루트 LICENSE 원문은 공식 URL에서 열어 확인했다. 고정 태그의 원문, 최신 release 응답, tag commit, README, NOTICE 유무는 `collect_metadata.py`가 `sources/`와 `metadata.json`에 URL·SHA-256과 함께 보관했다. 이 기록은 실험 도구 자체에 관한 것이며, 향후 외부 악성 사례·평가 데이터의 재배포 라이선스를 대신하지 않는다.

SkillSpector는 조사일 현재 버전 고정 PyPI 배포가 없어 공식 GitHub `v2.11.2` 태그에서 설치했다. pip가 확인한 commit은 `69dcdfb74487d361ba4c811d088cfdea2ff3a9dc`이고, 실행된 CLI도 `SkillSpector v2.11.2`를 반환했다. 설치 후 `pip freeze`, 각 도움말, 명령·입력 해시·stdout·stderr를 `runs/skillspector/`에 보관했다.

## 이번에 실행한 범위

| 도구 | 실행 모드 | 해석상 주의 |
|---|---|---|
| Cisco Skill Scanner | 로컬 디렉터리, static·bytecode·pipeline·correlation·behavioral analyzer | `behavioral`은 제품 내부 분석기 이름이며 실제 피해 모델의 런타임 실행이 아니다. |
| Cisco MCP Scanner | 고정 JSON 입력, YARA static | 도구 metadata와 resource 응답을 따로 검사했다. 외부 LLM analyzer는 실행하지 않았다. |
| NVIDIA SkillSpector | 로컬 디렉터리, `--no-llm`; 별도 `--fail-on-incomplete` gate | 구성요소 coverage는 100%였지만 workspace 형태 경로의 reference resolution 때문에 completeness는 partial이었다. strict gate는 정상 S00을 포함한 네 사례를 모두 exit 1로 차단했다. |
| AgentShield | 로컬 디렉터리 scan | 네 실행 모두 `filesScanned=0`이므로 A/100 출력을 안전 판정으로 사용하지 않는다. |
| Snyk Agent Scan | NOT_RUN | `SNYK_TOKEN`이 필요한 서비스 모드는 자격 증명 없이 실행하지 않았다. |

원본 입력이 실제 평가된 경우의 미발견만 `NOT_DETECTED`로 분류한다. `NOT_ASSESSED`, `NOT_RUN`, `ERROR`는 미탐 수치에 포함하지 않는다.
