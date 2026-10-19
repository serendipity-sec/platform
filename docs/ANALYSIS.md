# A: 그래프·변경·영향 경로 분석

팀장 Handoff의 전후 workflow와 노드 의미 정보로 `analysis.json`을 만든다.
workflow를 실행하지 않는다. 모든 간선과 경로는 **정적 가능성**(`STATIC_POSSIBILITY`)이며,
실제 실행 효과나 보안 위험 판정이 아니다. 공개 함수는
`analyze(handoff, limits=AnalysisLimits()) -> AnalysisBundle`이다.

## 재현

출력 파일은 아직 없어야 하고 상위 폴더는 있어야 한다.

```bash
mkdir -p var/a
uv run --frozen --extra baseline flowdelta analyze examples/contracts/support-triage/input \
  --out var/a/analysis.json
```

## B·C가 읽는 순서

1. `extensions["flowdelta.graph.limits"].truncated`가 `true`면 일부 계산을 생략했거나 경로 생성을 상한에서 멈춘 것이다. 이유는 `reasons`에 있다.
2. `changes`에서 변경과 원본 위치(`before_source`·`after_source`의 artifact ID·JSON Pointer)를 읽는다.
3. `impact_paths`에서 변경(`change_ids`)이 닿을 수 있는 출력까지의 경로를 읽는다. `edge_ids`를 `side`의 그래프(`before_graph`·`after_graph`)에서 찾아 노드를 따라간다.
4. 경로의 `support`가 `SUPPORTED`가 아니면, 경로 `limitations`에 이유와 해당 노드가 있다.
5. 분석 전체의 `limitations`에는 계산 범위와 생략한 것이 있다.

**경로가 없거나 비어 있다는 것은 안전하다는 뜻이 아니다.** 노드 상한 초과, 의미 정보 없음, 표시 변경, workflow 수준 변경은 각각 `limitations`에 남는다.

## 그래프

| 간선 | 만드는 근거 | 읽을 때 |
|---|---|---|
| `CONTROL` | 정규화 workflow의 연결 | `output_type`과 `input_type`이 **모두** `main`이면 실행 순서다. 그 밖의 연결(`ai_tool`·`ai_languageModel` 등)은 agent에 하위 노드를 붙이는 연결이며 실행 순서가 아니다. 계약의 `Connection`은 두 값이 다른 연결도 받으므로 한쪽 값만으로 판단하지 않는다. 검증기와 맞추려고 같은 `CONTROL`로 두고 두 값으로 구분한다(`flowdelta.graph.control.is_main`). |
| `DATA` + `extensions["flowdelta.graph.data_origin"]="NAME_REFERENCE"` | 파라미터 문자열의 `$('이름')`·`$node["이름"]`·`$items("이름")` | 참조되는 노드 → 참조하는 노드. `target_fields`·`evidence`는 참조 문자열의 JSON Pointer다. |
| `DATA` + `"FIELD_FLOW"` | 의미 정보의 `reads`/`writes` | 쓴 노드 → 읽은 노드. `source_fields`·`target_fields`는 쓴·읽은 `$json` 필드다. |

필드 흐름은 아래 규칙으로만 만든다.

- 읽는 노드에서 두 값이 모두 `main`인 `CONTROL` 간선만 거꾸로 따라가, 갈래마다 그 필드를 쓰는 가장 가까운 앞 노드를 찾는다. 상위 객체와 하위 필드(`$json.body` ↔ `$json.body.id`)도 같은 필드로 본다.
- `$json.*:unchanged-item`(If·Merge)과 `$json.*:preserved-input`(Include Other Fields) 노드는 건너뛰고 더 앞을 본다.
- `$json.*:array-element`(Split Out)·`$json.*:caller-input`(하위 workflow 진입)은 모든 필드를 쓰는 것으로 본다.
- 그대로 넘긴다는 선언이 없는 노드, 의미 정보가 없거나 지원되지 않는 노드, 오류 출력 포트에서는 멈추고 그래프 `limitations`에 위치와 이유를 남긴다.

고정 하위 workflow는 `dependency_graphs[<dependency_id>]`에 같은 규칙으로 따로 만든다. 호출 노드와 하위 workflow 사이의 필드 전달은 연결하지 않는다.

## 변경

- 노드 대응은 같은 ID만 쓴다. ID가 바뀐 노드는 추가·삭제로 남기고 구조 대응을 추측하지 않는다.
- 파라미터·연결·설정·credential·버전·표시(위치·메모·색) 변경을 원본 위치와 함께 남긴다.
- 노드 이름 변경은 종류를 `UNKNOWN`으로 두고, 참조 판정을 `extensions["flowdelta.graph.name_references"]`에 남긴다. `status`는 `NO_REFERENCES`·`REFERENCES_UPDATED`·`NEW_REFERENCES`·`BROKEN_REFERENCES`·`REFERENCES_RETARGETED`다.

## 영향 경로

| 필드 | 뜻 |
|---|---|
| `side` | 경로를 찾은 그래프. 삭제된 노드·연결은 `before`, 나머지는 `after` |
| `change_ids` | 같은 (side, 끝점, 간선 목록)에 걸리는 변경을 묶은 목록 |
| `edge_ids` | 시작 노드에서 끝점까지 순서대로. 변경 노드가 곧 끝점이면 빈 목록 |
| `support` | 시작 노드의 의미 정보가 없거나 지원되지 않으면 `UNKNOWN`, 경로 위 노드가 모두 지원되면 `SUPPORTED`, 그 밖은 `PARTIAL` |
| `task_ids` | Handoff 업무 ID |
| `policy_rule_ids` | 지금은 항상 비어 있다. 정책 규칙이 자연어뿐이라 노드와 연결할 근거가 없어서이며, 이유는 경로 `limitations`에 있다 |

- 시작점: 노드에 걸린 변경. 연결 변경은 바뀐 연결을 첫 간선으로 삼는다. 이름 변경으로 끊긴 참조는 수정 전 그래프의 그 참조 간선에서, 옛 이름을 다른 노드가 가져간 참조는 수정 후 그래프의 새 참조 간선에서도 출발한다.
- 끝점: 입력(`receive:`)이 아닌 효과가 있는 노드, 효과를 알 수 없는 노드, 나가는 `CONTROL` 간선이 없는 마지막 노드.
- 경로: `CONTROL`·`DATA` 간선을 앞으로 따라가는 끝점별 최단 경로 하나. 동점이면 간선 ID 순으로 정해서 같은 입력이면 같은 경로가 나온다. 가능한 모든 경로나 실제 실행 경로의 증거가 아니다.
- 만들지 않는 것: 표시 변경(`DISPLAY_CHANGED`)과 노드가 없는 workflow 수준 변경. 개수만 분석 `limitations`에 남긴다.
- 하위 workflow 안으로는 들어가지 않는다. 하위 workflow 호출이나 두 값이 모두 `main`은 아닌 연결(`ai_*` 등)을 지나는 경로에는 그 사실을 적는다.

## 분석 상한

설계 문서 §7.3의 개발 기본값이다. 파일럿에서 조정하며 성능 실적이 아니다.

| 상한 | 기본값 | 넘으면 |
|---|---|---|
| `max_workflow_nodes` | 100 | 그래프(CONTROL·이름 참조)와 변경은 그대로, 필드 흐름과 영향 경로는 계산하지 않음 |
| `max_snapshot_nodes` | 300 (본 workflow + 고정 하위 workflow) | 위와 같음 |
| `max_impact_paths` | 20 | 20개까지만 만든다. 변경마다 가장 짧은 경로를 먼저 하나씩 고르고 나머지를 채우며, 상한을 넘는 새 경로가 보이면 생성을 멈춘다 |

`extensions["flowdelta.graph.limits"]`에는 상한 값, `truncated`, `reasons`(`WORKFLOW_NODE_LIMIT`·`SNAPSHOT_NODE_LIMIT`·`IMPACT_PATH_LIMIT`), `impact_paths`(`computed`·`complete`·`total`·`kept`)가 항상 들어간다.

- `complete`가 `true`면 경로를 전부 만든 것이고 `total`이 정확한 전체 수다.
- `complete`가 `false`면 경로 상한에서 생성을 멈췄거나, 노드 상한 때문에 계산하지 않은 것이다(이때는 `computed`도 `false`). 전체 경로 수를 세지 않았으므로 `total`과 `IMPACT_PATH_LIMIT`의 `count`는 `null`이다.

## 아직 하지 않는 것

- 권한·능력, 의존성·보호, 지시 영향 관계. `ai_*` 연결을 이 관계로 나누려면 노드 의미 정보와 검증기 범위 조정이 필요하다.
- 정책 규칙 연결(`policy_rule_ids`). 정책·자원 대응표 형식이 정해지면 연결한다.
- ID가 바뀐 노드의 구조 대응, `code` 노드 안의 의미, 실제 모델 행동.
- 공용 DB 저장. 파일·메모리 저장소는 PR #54에 있고, DB adapter는 공통 migration 틀이 정해지면 같은 인터페이스로 붙인다.
