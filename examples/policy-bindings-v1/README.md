# 정책 binding v1 예제

이 디렉터리는 **입력·참조·노드 연결 검증용**이다. 정책 위반 정답이나 실행 결과가 아니다.
`fixtures.json`의 workflow 두 개는 팀이 새로 쓴 무해한 합성 JSON이다. Set 노드에
기억 삭제 기능이 있다는 주장이 아니다. 공개 n8n-claw 코드를 복제하거나 실행하지 않는다.

## Research policy

합성 개인 기억 `memory_long`의 `id`, `scope`, `owner_user_id`를 보호한다. 선언된
주체는 `after/merge-input` 출력의 `qualifiedUserId`다. 비소유자의 개인 기억 삭제를
금지한다. 이 fixture에는 **관리자 예외가 없다**. 이는 연구자 정의 정책이며 원제품의
운영 정책 또는 실제 인증/필드 전달을 입증하지 않는다.

| fixture | 기대 결과 |
|---|---|
| connected | after의 tool-memory-delete 경로에 memory-owner-delete 연결 |
| missing-before | before에 노드가 없고 after에는 존재: 대상 보존, 분석 한계 1개, 연결 없음 |
| unknown-node | 어느 쪽에도 없는 노드: POLICY_TARGET_NODE_UNKNOWN |
| wrong-pointer | 선언 side/node와 원본 위치 불일치: POLICY_BINDING_POINTER_MISMATCH |
| pointer-on-missing-side | 존재하지 않는 side에서 pointer를 주장: POLICY_BINDING_POINTER_MISMATCH |
| unknown-rule | 정책 rules에 없는 ID: POLICY_TARGET_RULE_UNKNOWN |

`/nodes/1/parameters/options`는 합성 workflow의 위치다. 공개 원본의 `/nodes/29`와
혼용하지 않는다. 원본을 바꾸거나 배열 순서를 옮겼다면 pointer도 새 원본으로 검증해야 한다.

`order-bindings.json`은 기존 주문 예제를 **복사한 임시 입력**에만 주입한다.
`request.headers.x-fixture-subject`는 이 테스트가 선언한 주체 필드이며 기존 주문 서비스가
이를 인증한다는 뜻이 아니다. 노드 참조 확인과 실제 주체/소유권 확인을 구분하는 예다.
원본 reference bundle, 실행 allowlist, 과거 실행 증거는 바꾸지 않는다.

```powershell
uv run --frozen --extra baseline --extra knowledge pytest tests/test_policy_bindings.py
```

#61의 실제 코드와 조합하는 명령 및 한계는 [POLICY_BINDINGS.md](../../docs/POLICY_BINDINGS.md)를 따른다.
