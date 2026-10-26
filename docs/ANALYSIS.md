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

`analysis_id`는 전후 snapshot ID, `producer`, 결과 내용의 해시로 만든다. 같은 입력과 같은 분석기면 같은 ID이고, 결과가 다르면 ID도 다르다.

**경로가 없거나 비어 있다는 것은 안전하다는 뜻이 아니다.** 노드 상한 초과, 의미 정보 없음, 표시 변경, workflow 수준 변경은 각각 `limitations`에 남는다.

**B는 A에서 추출한 분석 맥락을 보존하고 영향 경로 정보를 검색에 활용한다.**
- `RetrievalResult.query.analysis_context`와 `TestPlan.planner_metadata.analysis_context`에 경로, 변경과 원본 위치, 제한 및 상한 정보를 보존한다.
- 검색 점수에는 경로에서 찾은 노드 종류와 변경 필드 키워드를 반영하고, 검색 hit 사유에는 일치한 `path_id`를 남긴다.
- 계획에는 분석의 경로 맥락이 들어가지만, B가 한 계획에서 검사할 단일 `path_id`를 선택하지는 않는다. `TestPlan`의 상위 계약에 선택 경로 필드가 없고, C의 개별 경로 소비·검증 연결은 아직 구현되지 않았다.

## 원본 위치까지 잇기 (C)

실행 결과를 A의 변경과 원본 파일 위치까지 거슬러 갈 때는 아래 순서로 ID를 맞춘다.

1. **실행 → 계획**: `ExecutionRun.plan_id` = `TestPlan.plan_id`
   - `ExecutionRun.side`가 `before`면 `ExecutionRun.snapshot_id` = `TestPlan.before_snapshot_id`, `after`면 `after_snapshot_id`다.
   - 모듈 산출물 bundle 검증이 이 둘을 확인한다(`RUN_CONTEXT_MISMATCH`).
2. **계획 → 분석**: `TestPlan.analysis_id` = `AnalysisBundle.analysis_id`. 파이프라인 검증(`validate_pipeline`)이 이 ID와 전후 snapshot ID가 같은지 확인한다(`ANALYSIS_ID_MISMATCH`·`SNAPSHOT_ID_MISMATCH`).
3. **분석 → 변경·경로**:
   - 경로의 `change_ids`로 `changes[]`를 찾아 `node_id`와 `before_source`·`after_source`를 읽는다.
   - `edge_ids`는 경로 `side`의 그래프(`before_graph`·`after_graph`)의 `edges[]`에서 찾는다. 노드는 `source_node_id`·`target_node_id`, 연결의 원본 위치는 `evidence`에 있다.
   - 변경 노드에서 바로 끝나는 경로는 `edge_ids`가 비어 있다.
4. **원본 위치 → 파일**:
   - `SourceLocation.artifact_id`를 Handoff `source-manifest.json`의 `artifacts[]`에서 찾아 `path`·`sha256`을 얻는다.
   - 파일은 Handoff 폴더의 그 `path`에 있다(파이프라인 bundle에서는 `input/<path>`). `json_pointer`는 그 파일 안의 위치다.

### 예: 문의 분류 예제(`examples/contracts/support-triage/`)

| 단계 | 파일 | 맞추는 값 |
|---|---|---|
| 실행 | `run.example.json` | `plan_id=plan-support-triage-example`, `side=before`, `snapshot_id=snapshot-55c0bfc6…` |
| 계획 | `plan.json` | 같은 `plan_id`, `before_snapshot_id=snapshot-55c0bfc6…`, `analysis_id=analysis-support-triage-example` |
| snapshot | `input/snapshots.json` | `before` = `snapshot-55c0bfc6…`(workflow `support-source-10`), `after` = `snapshot-c183fc3e…`(`support-source-9`) |
| 경로 | `flowdelta analyze`로 만든 `analysis.json` | `impact_paths[0]`: `side=after`, `change_ids=[change-48e35cb38cd5e8ff]`, `edge_ids=[]` |
| 변경 | 같은 `analysis.json`의 `changes[]` | `change-48e35cb38cd5e8ff`: `PARAMETER_CHANGED`, `node_id=result`, `after_source` = `support-source-9` + `/nodes/8/parameters/assignments/assignments/2` |
| 간선 | 같은 파일의 `after_graph.edges[]` | 예: `control-start-seed-5f4dc851`(`start` → `seed`), `evidence` = `support-source-9` + `/connections/Manual request/main/0/0` |
| 파일 | `input/source-manifest.json` | `support-source-9` → `workflow/after.json`. `/nodes/8`은 그 파일의 `result`("Triage result") 노드다 |

- `edge_ids=[]`인 것은 변경 노드 `result`가 곧 끝점(마지막 노드)이기 때문이다.
- 저장소의 `analysis.json`·`plan.json`은 계약 예제(`CONTRACT_EXAMPLE`)라 `impact_paths`가 비어 있다. 그래서 경로·변경·간선 단계는 같은 입력을 `flowdelta analyze`로 분석한 결과다.
  - 실제 파이프라인에서는 B가 그 생성 분석으로 계획을 만들고, 계획의 `analysis_id`도 생성 분석의 ID가 된다.
  - 생성 분석의 `analysis_id`·`path_id`는 결과 내용에서 만들므로 A 버전에 따라 달라질 수 있다.

### 아직 연결되지 않은 것 (미구현)

- **실행 결과 → 검사한 영향 경로**: B는 `analysis_context`의 모든 경로 참조를 계획에 보존하지만, 한 실행에서 검사할 단일 `path_id`를 선택하지 않는다. 따라서 실행 결과는 어느 경로를 검사했는지 기록하거나 검증하지 못하며, C의 경로별 소비는 아직 연결되지 않았다.
- **실행 관측 → 변경 원본 위치**: `ExecutionRun.artifacts`는 실행 산출물이다. 관측한 현상이 어느 변경·노드에서 왔는지 직접 잇는 필드는 없다. 위 순서는 실행이 다룬 분석과 그 분석의 변경을 찾는 것까지다.
- **경로 → 정책 규칙**: `policy_rule_ids`는 비어 있다(정책 binding 형식 합의 후 #61).

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
| `path_id` | 경로 ID. `side`·끝점·`edge_ids`의 해시라 같은 입력과 분석기면 같은 값이다. 한 분석 안에서 경로를 가리킬 때 쓴다 |
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
| `max_impact_paths` | 20 | 20개까지만 만든다. 변경 ID 순으로 돌며 변경마다 가장 짧은 경로를 먼저 하나씩 고르고 나머지를 채운다. 같은 간선 수면 BFS가 먼저 찾은 끝점(간선 ID 순)부터 고른다. 상한을 넘는 새 경로가 보이면 후보 생성을 멈춘다 |

`extensions["flowdelta.graph.limits"]`에는 상한 값, `truncated`, `reasons`(`WORKFLOW_NODE_LIMIT`·`SNAPSHOT_NODE_LIMIT`·`IMPACT_PATH_LIMIT`), `impact_paths`(`computed`·`complete`·`total`·`kept`)가 항상 들어간다.

- `complete`가 `true`면 경로를 전부 만든 것이고 `total`이 정확한 전체 수다.
- `complete`가 `false`면 경로 상한에서 생성을 멈췄거나, 노드 상한 때문에 계산하지 않은 것이다(이때는 `computed`도 `false`). 전체 경로 수를 세지 않았으므로 `total`과 `IMPACT_PATH_LIMIT`의 `count`는 `null`이다.

## 아직 하지 않는 것

- 권한·능력, 의존성·보호, 지시 영향 관계. `ai_*` 연결을 이 관계로 나누려면 노드 의미 정보와 검증기 범위 조정이 필요하다.
- 정책 규칙 연결(`policy_rule_ids`). 정책·자원 대응표 형식이 정해지면 연결한다.
- ID가 바뀐 노드의 구조 대응, `code` 노드 안의 의미, 실제 모델 행동.
- 공용 DB 저장. 파일·메모리 저장소(`flowdelta.graph.repository`)는 main에 있고, DB adapter는 같은 인터페이스로 따로 붙인다.
