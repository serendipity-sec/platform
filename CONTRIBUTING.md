# 기여 가이드

## 브랜치 전략

`main` 하나를 기준으로 하는 GitHub Flow를 씁니다. `develop` 브랜치는 두지 않습니다.

```
main ──●────────●────────●──      항상 동작하는 상태. 직접 push 금지
        \      /  \     /
         ●────●    ●───●          feature 브랜치 → PR → 리뷰 → squash merge
```

브랜치 이름: `<type>/<issue번호>-<짧은-설명>`

```
feat/12-skill-md-parser
fix/31-graph-edge-dedup
docs/07-threat-model
```

`type`: `feat` `fix` `docs` `refactor` `test` `chore` `experiment`

## 커밋 메시지

Conventional Commits를 씁니다. 주차별 활동 이력을 정리할 때 그대로 근거가 됩니다.

```
<type>(<scope>): <한 줄 요약>

<본문 — 왜 이렇게 했는지. 무엇을 했는지는 diff가 말해 줍니다>

Refs: #12
```

예시

```
feat(scanner): SKILL.md frontmatter에서 allowed-tools 추출

allowed-tools 선언은 호스트의 실제 권한 강제와 다르므로
결과 모델에 declared_only 플래그를 함께 저장한다.

Refs: #12
```

## Pull Request

- PR 하나는 리뷰 가능한 크기로 유지합니다. 변경 파일이 20개를 넘어가면 나눌 수 있는지 먼저 봅니다.
- **리뷰어 1명 이상의 승인** 후 머지합니다. 본인 PR을 본인이 승인하지 않습니다.
- 머지는 **Squash and merge** 로 통일합니다. `main` 이력이 PR 단위로 읽힙니다.
- 머지 후 브랜치는 삭제합니다.

## 자동 검사

PR을 올리면 아래가 자동으로 돕니다. 빨간불이면 머지하지 않습니다.

| 검사 | 실패하면 |
|---|---|
| `pr-check` | PR 제목을 `<type>(<scope>): <요약>` 형식으로, 본문에 `Closes #이슈번호` 추가 |
| `approval` | 팀원 1명에게 리뷰 요청. Approve 받으면 자동으로 초록불. 승인 없이 머지하면 주간 현황에 표시됩니다 |
| `secret-scan` | 토큰·키를 커밋에서 빼고 해당 키는 폐기·재발급 |
| `semgrep-cloud-platform/scan` | Semgrep 앱이 직접 검사합니다(워크플로 파일 없음). PR 댓글과 [semgrep.dev](https://semgrep.dev/orgs/-/findings) 대시보드에서 확인하고 수정. 오탐이면 대시보드에서 Ignore 처리 |
| `auto-label` | 실패할 일 없음. 경로별 라벨과 담당자(작성자)를 자동으로 붙입니다 |

`main`에 PR 없이 push하면 `main-push-guard`가 그 커밋에 댓글을 남깁니다.
Free 플랜 private 저장소라 브랜치 보호를 켤 수 없어서, 막는 대신 알립니다.

## 이슈

모든 작업은 이슈에서 시작합니다. 이슈 없이 만든 브랜치는 리뷰 대상이 아닙니다.
이슈는 GitHub Projects 보드에 올려 주차 단위로 관리합니다.

## 검사 대상 자료를 다룰 때

이 프로젝트는 **신뢰할 수 없는 입력**을 다룹니다. 다음을 지킵니다.

1. 검사 대상 MCP 서버·플러그인·스크립트를 **실행하지 않습니다.** 정적으로만 읽습니다.
2. 테스트용 악성 예제는 **실제 악성코드를 쓰지 않고**, 무해한 지침 문구와 정적 코드 조각으로 만듭니다.
3. 검사 대상 저장소에서 발견한 비밀정보(토큰·키)는 보고서와 커밋에 남기지 않습니다.
4. 외부 LLM 분석을 붙일 경우, 원문이 어디까지 전송되는지 코드 주석과 문서에 명시합니다.
5. 검사에 쓰는 모델에는 **실행 도구를 주지 않습니다.**

## 라이선스

외부 코드·규칙·데이터셋을 가져다 쓸 때는 반드시 [THIRD_PARTY.md](THIRD_PARTY.md)에 먼저 기록합니다.
과목 규정상 **오픈소스 라이선스 미준수는 50% 감점**입니다. 출처 표기 없는 복사는 표절로 0점 처리됩니다.
