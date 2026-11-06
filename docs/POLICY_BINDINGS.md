# 명시적 정책 binding v1

Refs #48, #49, #61. `OBJECT_OWNERSHIP`과 노드 연결/비대칭/잘못된 참조 구분은
[#48의 10월 26일 결정](https://github.com/serendipity-sec/platform/issues/48#issuecomment-6407090713)을 따른다.
입력과 공개 resolver가 참조를 검증하고, `analyze()`가 명시적 대상 노드를 지나는 영향 경로에
규칙 ID를 연결한다. 정책 위반 판정, 주체 인증, JS 의미 해석은 포함하지 않는다.

## 입력과 분석의 연결

#84의 입력 binding과 #61의 A 소비를 결합한다. `analyze()`는 경로 생성 상한을 확인하기 전에
참조를 검증하고, 경로 생성 뒤 정책 연결을 적용한 다음 분석 내용 ID를 계산한다.
binding이 없는 입력은 이전 분석 결과를 바이트 단위로 유지한다.

[등록 주문 실행 예제](../examples/module-outputs/profiled-runner/README.md)는 binding 없는 정상
업무 실행이다. 아래 정책 통합 명령은 binding을 포함한 합성 입력의 실제 A 분석과 파일 저장,
B 근거 소비를 별도로 확인하며 정책 위반이나 C의 정책 집행을 검증하지 않는다.

## 위치와 버전·호환성

`policy.json`의 선택적 `bindings` 필드는 `{ "version": "1.0.0", "bindings": [...] }`다.
내부 항목은 `ObjectOwnershipBinding`, 독립 schema는 `schemas/policy-bindings-v1.schema.json`이다.
알 수 없는 version/kind/필드(위반 라벨 포함)는 거부한다. 임의 `extensions`에 넣어 묵시적으로
무시하는 방식이 아니다. 정책 전체는 기존 manifest 해시 및 snapshot/profile 고정 범위에 포함된다.

| 생산자/소비자 | 변경과 호환성 |
|---|---|
| 기존 0.4.0 unbound 입력 | 새 reader에서 동일하게 처리. bindings=None은 직렬화에서 생략하여 기존 해시/예제 보존 |
| 새 입력 생산자 | policy.bindings.version을 명시. 정상·예외·근거를 작성하고 manifest/profile을 새 입력으로 고정 |
| 기존 strict reader | 새 bindings 필드를 extra_forbid로 거부. 새 입력을 구버전에 보내면 실패하며 조용히 연결을 생략하지 않음 |
| load_case/build_handoff/read_handoff | rule/side/node/pointer/subject 검증. 재조회 시 raw workflow와 normalized의 node/source/parameters도 비교 |
| A 소비자 | analyze()가 resolver의 targets를 PolicyTarget으로 변환하고 내용 ID 계산 전에 연결 |
| B | 연결된 impact_paths.policy_rule_ids 및 analysis_context 보존. 소유권 판정이나 예외 평가 기능은 추가하지 않음 |
| C | 정책 내용 전달 가능. 실행 허가/정적 finding/보안 판정은 별도 계약이며 이 PR이 승인하지 않음 |
| 입력/분석 저장 | policy 원문과 manifest 해시 보존, 새 binding은 새 snapshot/profile/analysis를 요구. DB column/migration 변경 없음 |
| 공통 schema/예제 | input schema 재생성, 독립 v1 schema와 합성 fixture 추가. pipeline schema와 기존 reference bundle은 의미·bytes 변경 없음 |

기존 필드를 재해석하지 않는 선택적 기능으로 outer 0.4.0을 유지하고 inner v1을 고정한다.
기존 strict reader는 새 bound 입력을 거부한다. 새 생산자와 소비자는 함께 갱신해야 한다.

## 필드와 검증 경계

| 필드 | 의미·검증 |
|---|---|
| rule_id | 같은 policy.rules에 실제 존재하는 ID. 문장에서 추론하지 않음 |
| policy_kind | 현재는 OBJECT_OWNERSHIP만 허용. P3 승인 정책으로 바꾸지 않음 |
| applies_to | 명시적 side와 node_id. 동일 rule의 여러 대상은 이 배열에 선언 |
| json_pointer | 선택적 **해당 side의 원본 workflow** 위치. 지정 노드 자체 또는 그 노드 parameters 아래 실제 존재하는 값만 허용 |
| protected_resource | 업무 자원 이름과 데이터 fields. workflow pointer가 아니며 DB 존재/소유권은 아직 검증하지 않음 |
| subject | side/node_id/선택적 원본 pointer + 출력 데이터 field_path. source 노드는 해당 side에 반드시 존재. 필드 존재·인증/신뢰는 미검증 |
| forbidden_effect | operation과 condition을 분리한 선언. 실행하거나 A의 effects와 자동 일치 판정하지 않음 |
| allowed_exceptions | 빈 배열도 필수. condition과 별도 evidence_refs. 최소 1개 연구 정책/업무 요구 근거 필수, 구현 코드만으로 예외 허용 불가 |
| evidence_refs | RESEARCH_POLICY / BUSINESS_REQUIREMENT / PUBLIC_IMPLEMENTATION, reference, rationale. 참조 문자열을 자동 fetch/실행하지 않음 |

legacy `PolicyRule.exceptions`와 구조화 예외의 condition 목록은 정확히 같아야 한다.
자연어의 의미 동등성을 추측하지 않는다. resource/subject/condition의 legacy 설명과 구조화
선언의 의미 일치 여부는 작성자/리뷰어가 확인해야 한다. 규칙 당 binding은 하나이며
동일 side/node 대상 중복을 거부한다. 모든 규칙에 binding을 강제하지는 않는다.

node_id가 선언 side에는 없고 반대쪽에는 있는 경우, **pointer를 생략한 node-only 선언**은
정상 비대칭이다. resolver는 그 대상도 보존하고 분석 한계를 반환한다. 없는 side에 pointer를
달거나 어느 쪽에도 노드가 없으면 거부한다. 배열 위치를 반대편에서 빌리지 않는다.
지원 pointer 범위를 node/parameters 외 metadata까지 확대할지, 주체 source의 비대칭도 허용할지는
별도 검토 사항이다. 현재는 명시적으로 좁은 범위를 거부/허용하며 자동 확장하지 않는다.

## A의 소비 순서

다음 처리는 `analyze()` 내부에서 수행한다. 호출자가 분석 결과에 다시 연결하거나 내용 ID를
수동으로 덮어쓰지 않는다.

```python
resolution = resolve_policy_bindings(handoff)
targets = [PolicyTarget(t.rule_id, t.side, t.node_id) for t in resolution.targets]
paths, notes = link_policies(
    paths, changes, {"before": before_graph, "after": after_graph},
    targets, {rule.rule_id for rule in handoff.policy.rules},
)
limitations.extend(notes)  # 분석 한 곳. resolution.limitations와 중복 추가하지 않음.
# field/effect/subject/exception 평가 미지원 한계도 유지.
# 이후 bundle을 만든 뒤 identified(bundle); 기존 analysis_id를 재사용하지 않음.
```

연결은 **실제 analyze() 안에서 내용 ID 계산 전에** 적용한다.
경로 생성이 상한으로 생략된 경우에도 입력 참조 검증은 실행한다. binding이 없는 입력의
기존 POLICY_NOTE, 상한·support·STATIC_POSSIBILITY 의미를 보존한다. 연결된 경로에서만
미연결 문구를 제거하고 분석 전체의 오래된 “정책 규칙과는 연결하지 않았다” 설명을 갱신한다.
알 수 없는 rule/task ID는 `validate_pipeline()`에서 추가로 거부한다.

아래 명령으로 실제 analyze → 정확한 ID의 A 저장/재조회 → B plan 근거 소비를 확인한다.
공용 PostgreSQL 및 실제 C 소비는 이 PR의 파일 저장 통합 검증으로 대체하지 않는다.

## 재현과 실제 검증 범위

`tools/verify_policy_binding_integration.py`는 설치된 `analyze()` → **실제 FileAnalysisRepository**
save/get → **실제 B write_plan** → validate_pipeline을 수행한다. 경로 연결과 내용 ID는 A의
실제 공개 진입점이 생성하며 스크립트가 다시 계산하지 않는다. memory 합성 fixture 6개는
별도 resolver/linker 검사로 확인한다. 결과에는 `security_outcome=null`을 기록한다.

```powershell
uv run --frozen --extra baseline --extra knowledge python tools/verify_policy_binding_integration.py --out var/policy-binding-integration
uv run --frozen --extra baseline --extra knowledge pytest tests/test_policy_bindings.py
uv run --frozen --extra baseline --extra knowledge python tools/refresh_contract_artifacts.py --schemas-only
```

출력 폴더가 이미 있으면 새 폴더 이름으로 실행한다. 실제 n8n/LLM, C scanner, 공유 DB는 실행하지 않는다.
합성 주문의 subject header는 인증된 주체의 증거가 아니다. 현재 검증은 노드 연결의 소비 증거다.

전체 artifact refresh는 runtime source pin까지 갱신하므로 이번 선택적 schema 변경에는
`--schemas-only`를 쓴다. 과거 실행 해시를 새 실행 없이 바꾸지 않는다.
