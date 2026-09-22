# 추가 통제 테스트를 위한 공식 자료 확인

확인일: **2026-09-22**. 공식 사양·공식 저장소·공식 릴리스만 사용했다. 이 문서의 버전은 조사 시점 snapshot이며 이후 최신성을 보장하지 않는다.

## MCP 도구 목록 변경과 승인 binding

확인한 최신 안정 MCP 사양은 `2026-07-28` GA다.

- [공식 릴리스](https://github.com/modelcontextprotocol/modelcontextprotocol/releases/tag/2026-07-28)
- [변경사항](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)
- [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Security Best Practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)

공식 사양에서 도출되는 테스트 입력은 다음과 같다.

- `tools/list`는 authorization context에 따라 달라질 수 있으므로 승인 기록에 server identity·endpoint·protocol version·authorization scope·cache context를 함께 묶어야 한다.
- 2026-07-28 목록은 `ttlMs`, `cacheScope`를 사용한다. `private` catalog를 다른 사용자·토큰에 재사용하면 안 된다.
- `subscriptions/listen`의 `toolsListChanged` 신호와 TTL 만료는 캐시를 무효화하고 재조회하게 한다.
- 도구 순서만 바뀐 무해 대조군과 description·schema·annotations·도구 추가·권한별 목록 변경을 분리해야 한다.
- 변경 시 재승인은 MCP가 직접 강제하는 표준 규칙이 아니라, 변경 신호·사람의 확인·metadata 비신뢰 원칙을 연결한 우리 제품 정책이다.

현재 v2.0 lab은 `2025-06-18` minimal stdio와 명시적 두 번 조회로 canonical tool contract digest만 검증한다. 최신 MCP subscription·TTL·authorization 적합성 시험이 아니며 endpoint와 권한 context도 아직 digest에 포함하지 않는다. 이 제한을 `defense-results.md`에 유지한다.

## Agent Skills 패키지 완전성

- [Agent Skills specification](https://agentskills.io/specification)
- [client implementation](https://agentskills.io/client-implementation/adding-skills-support)
- [공식 저장소](https://github.com/agentskills/agentskills)
- [`skills-ref`](https://github.com/agentskills/agentskills/tree/main/skills-ref)
- [scripts 사용](https://github.com/agentskills/agentskills/blob/main/docs/skill-creation/using-scripts.mdx)

Skill은 최소 `SKILL.md` 외에도 scripts·references·assets와 임의 추가 파일을 포함할 수 있고 서로 다른 시점에 로드될 수 있다. `skills-ref validate`의 문서화된 범위는 frontmatter와 naming convention이므로 보안 완전성 판정으로 확대하지 않는다. `allowed-tools`도 experimental 정보이므로 실제 런타임 host 정책으로 다시 강제해야 한다.

후속 completeness fixture는 누락 참조, orphan executable, root 밖 symlink, archive path traversal, 과도한 파일 수·압축 해제 크기, activation 뒤 지연 로드되는 악성 reference를 포함한다. 문법 통과와 보안 통과를 별도 상태로 둔다.

## 전체 디렉터리 서명과 공급망 동일성

- [NVIDIA signed skills](https://github.com/NVIDIA/skills/blob/main/docs/signing-agent-skills.mdx)
- [Sigstore model transparency](https://github.com/sigstore/model-transparency)
- [cosign blob verification](https://docs.sigstore.dev/cosign/verifying/verify/)
- [SLSA v1.2 artifact verification](https://slsa.dev/spec/v1.2/verifying-artifacts)
- [TUF 1.0.26](https://theupdateframework.github.io/specification/v1.0.26/)
- [MCP Registry package types](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx)

NVIDIA 문서는 `skill.oms.sig`가 서명 파일 자체를 제외한 전체 Skill 디렉터리를 덮고, 서명 뒤 파일 추가도 strict verification에서 실패해야 한다고 설명한다. 서명은 안전성을 증명하는 판정이 아니라 승인된 디렉터리와 현재 디렉터리의 동일성 근거다.

SkillSpector는 OMS 구조를 인식하지만 자체적으로 signature·certificate chain·transparency log·signer identity를 검증하는 단계로 간주하지 않는다. 내용 검사와 서명자·무결성 검증을 분리해 실행해야 한다.

## 2026-09-22 스캐너 기준선

| 도구 | 확인한 최신 릴리스 | 이번 실험의 상태 |
|---|---:|---|
| [Cisco Skill Scanner](https://github.com/cisco-ai-defense/skill-scanner/releases/tag/2.1.0) | 2.1.0 | static+behavioral 실행 완료; LLM 모드 미실행 |
| [Cisco MCP Scanner](https://github.com/cisco-ai-defense/mcp-scanner/releases/tag/4.8.4) | 4.8.4 | YARA static 실행 완료; live dynamic registration 재시험 필요 |
| [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector/releases/tag/v2.11.2) | 2.11.2 | `--no-llm`·strict completeness 실행 완료 |
| [Snyk Agent Scan](https://github.com/snyk/agent-scan/releases) | 0.6.4 | 0.6.3 출처만 전날 고정; token 필요 분석은 계속 NOT_RUN |
| [AgentShield](https://github.com/affaan-m/agentshield/releases/tag/v1.6.0) | 1.6.0 | 임의 Skill 폴더에서 filesScanned=0; 지원 project layout 재시험 필요 |

Snyk Agent Scan은 2026-09-21에 0.6.4가 나와 전날 고정한 0.6.3이 더 이상 최신이 아니다. 기존 결과를 소급해 바꾸지 않고, 다음 scanner batch에서 새 버전을 별도 고정한다. `inspect`는 discovery이며 보안 분석인 `scan`과 구분한다.

## 다음 실행 우선순위

1. 최신 MCP `subscriptions/listen`·TTL·authorization context를 구현한 live M03 확장.
2. 전체 Skill bundle manifest·strict signature의 파일 추가/수정/잘못된 signer 시험.
3. scanner 종료·coverage·filesScanned·digest freshness를 fail-closed 결정으로 정규화하는 adapter contract.
4. 지원되는 AgentShield project layout과 Cisco MCP live-server mode 재시험.
5. 사전 고정한 악성/정상 near-neighbor mutation corpus와 다른 모델 계열 반복.

이 순서는 새 탐지 엔진을 만들기 전에 기존 통제의 강한 조합이 해결하는 범위와 우리 플랫폼이 연결해야 할 근거를 먼저 확인하기 위한 것이다.
