# n8n-claw 기억 삭제 사례: A 분석 재현

[#49 요청](https://github.com/serendipity-sec/platform/issues/49#issuecomment-6258500230)에 따라,
[#57](https://github.com/serendipity-sec/platform/pull/57)에 공유된 n8n-claw 비교 쌍 두 개를 A로
분석한다. B는 아래 명령으로 실제 `analysis.json`을 만든다. workflow는 실행하지 않는다.

## 재현

저장소 루트에서 실행한다. 출력 폴더 `var/claw-a`는 아직 없어야 한다.

```bash
uv run --frozen --extra baseline --extra knowledge python experiments/n8n-claw-a/build_cases.py --out var/claw-a
for pair in feature-add first-fix; do
  uv run --frozen --extra baseline --extra knowledge flowdelta snapshot var/claw-a/$pair/case --out var/claw-a/$pair/handoff
  uv run --frozen --extra baseline --extra knowledge flowdelta analyze var/claw-a/$pair/handoff --out var/claw-a/$pair/analysis.json
done
```

PowerShell에서는 반복문만 다르다.

```powershell
foreach ($pair in "feature-add", "first-fix") {
  uv run --frozen --extra baseline --extra knowledge flowdelta snapshot var/claw-a/$pair/case --out var/claw-a/$pair/handoff
  uv run --frozen --extra baseline --extra knowledge flowdelta analyze var/claw-a/$pair/handoff --out var/claw-a/$pair/analysis.json
}
```

- 첫 명령은 원본 4개를 고정 commit URL에서 받는다. 각 파일은 SHA-256과 크기를 확인한다. commit·해시는 #57 `sources.json`과 같다.
- #57의 `collect.py`로 이미 받았으면 `--sources var/claw-sources`를 붙인다. 그러면 네트워크를 쓰지 않는다.
- 원본 저장소에 별도 LICENSE가 없어(#57 조사) 아래 둘은 `var/`에만 두고 커밋하지 않는다.
  - 원문
  - `analysis.json`: 변경의 `before_value`·`after_value`에 원문 코드가 들어간다.

## 만드는 입력

| 쌍 | 원본 commit | `case_id` | 바뀐 것 |
|---|---|---|---|
| `feature-add` | `9ebf36b` → `63a7f2e` | `n8n-claw-memory-feature-add` | 기억 수정·삭제 도구와 AI 연결 추가 |
| `first-fix` | `4d1b1b7` → `eb6d7bc` | `n8n-claw-memory-first-fix` | 기억 도구에 소유자·관리자 검사 추가 |

- 두 쌍은 서로 다른 비교라 하나로 합치지 않는다.
- workflow(`workflow/before.json`·`after.json`)는 원본 바이트 그대로다.
- task·policy·environment·초기 상태·provenance는 **A 분석 재현용 임시 값**이다. 입력 담당의 고정 profile(#48·#56)이 이 사례를 받으면 그것으로 바꾼다.
  - environment는 팀 core adapter 기준(n8n 2.42.6, `core-local-v1`)이다. n8n-claw가 실제로 도는 버전이 아니다.
  - policy 규칙은 #57 README의 연구 정책(본인 기억만 수정·삭제, 관리자는 타인 기억 삭제 가능)을 옮긴 것이다.

## 결과

`changes`와 그래프는 A 코드 버전과 관계없이 같다. 영향 경로 수는 상한(#44)이 들어가면 바뀐다.
`analysis_id`는 결과 내용의 해시를 포함하므로(#65) A 버전마다 달라질 수 있다. 같은 코드로 다시 만들면 같다.

| | `feature-add` | `first-fix` |
|---|---|---|
| 변경 | `ADDED` Memory Delete(`/nodes/29`)·Memory Update(`/nodes/30`), `CONNECTION_CHANGED` 두 도구 → AI Agent `ai_tool`, `PARAMETER_CHANGED` Memory Search `description` | `PARAMETER_CHANGED` 6개: Memory Delete·Save·Search·Update의 `jsCode`, Delete·Save의 `description` |
| after 그래프 | `CONTROL` main 35·`ai_tool` 18·`ai_languageModel` 1, 이름 참조 `DATA` 27, 필드 흐름 0 | `CONTROL` main 44·`ai_tool` 20·`ai_languageModel` 1, 이름 참조 `DATA` 36, 필드 흐름 0 |
| 영향 경로(main) | 27개, 변경 5개 모두 경로 있음 | 44개, 변경 6개 모두 경로 있음 |
| 영향 경로(#44·#53 포함) | 20개, `complete=false` | 20개, `complete=false` |
| `support` | 모두 `UNKNOWN` | 모두 `UNKNOWN` |

## 읽을 때 주의할 것

- **`ai_tool`·`ai_languageModel`은 실행 순서가 아니다.** 검증기와 맞추려고 `CONTROL`로 남기고 `output_type`·`input_type`으로 구분한다. #53부터 이 연결을 지나는 경로에 그 사실을 적는다.
- **`UNKNOWN`과 경로가 없는 것은 안전하다는 뜻이 아니다.**
  - core adapter는 이 workflow의 노드 전부를 미지원(`WORKFLOW_SETTINGS_NOT_SUPPORTED`)으로 둔다. 그래서 효과·필드 흐름이 없다.
  - 검사 두 개에 모두 걸린다. workflow 설정의 `callerPolicy`·`availableInMCP`가 허용 목록에 없고, `ai_*` 연결도 지원하지 않는다. 설정만 고쳐도 `ai_*` 연결 때문에 `CONNECTION_PORT_NOT_SUPPORTED`로 미지원이다.
  - 효과를 모르는 노드는 모두 끝점이 된다. 그래서 경로가 많고 상한에 걸린다.
- **소유권 검사는 `jsCode` 안에 있다.** A는 "`jsCode`가 바뀌었다"와 그 원본 위치까지만 준다. 검사가 들어갔는지는 C의 정적 검사(#51)가 본다.
- `policy_rule_ids`는 비어 있다. 정책 binding 형식(#48)이 정해지면 연결한다(#61).

## 다음

- 고정 profile·입력 저장소(#56·#58)가 이 사례를 받으면, 임시 입력 대신 그 입력으로 같은 명령을 돌린다.
- 노드 의미(AI Agent, toolCode, 채팅 모델)가 생기면 `support`를 판단하고, agent가 도구를 부를 수 있다는 관계를 `CAPABILITY`로 바꾼다.
