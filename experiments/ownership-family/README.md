# 주문 조회 소유권 변형 계열 (#28 시연·평가 후보)

워크플로를 고친 뒤 **다른 고객의 주문이 나가는지**를 실제 n8n 실행으로 확인한다.
변형 4개에 입력 3개를 넣어 12번 실행했다. 판정에는 팀의 어댑터(#109)와 판정기(#103)를 수정 없이 썼다.

등록된 런타임(`runtime/order-lookup`, `src/flowdelta/runtime`)은 바꾸지 않았다. 이 실험은 별도 fixture 이미지와 compose 파일을 쓴다.

## 변형

모두 Webhook → HTTP Request → Respond 세 노드다. 등록 런타임이 허용하는 노드와 버전만 쓴다. 다른 점은 주문을 읽는 HTTP Request 한 곳이다.

| 변형 | 주문 읽는 곳 | 소유자 확인 | 의미 |
|---|---|---|---|
| V0 `v0-service-check` | `/orders/lookup` | 서비스가 함 | 등록된 `before.json` 그대로 |
| V1 `v1-internal-no-owner` | `/internal/orders/{id}` | 없음 | 회귀: 내부 경로로 바꾸면서 확인이 빠짐 |
| V2 `v2-internal-owner-fixed` | `/internal/orders/{id}?owner=customer-demo` | 인증된 고객으로 고정 | 제대로 고침 |
| V3 `v3-internal-owner-from-body` | `/internal/orders/{id}?owner={{body.customer_id}}` | 요청 본문의 값 | 덜 고침: 요청자가 소유자를 사칭할 수 있음 |

`make_variants.py`가 등록된 `before.json`에서 V1~V3을 결정적으로 만든다.

## 입력

| 입력 | 본문 | 뜻 |
|---|---|---|
| `normal` | ORD-1001, customer-demo | 정상 업무 |
| `other-order` | ORD-2001, customer-demo | 남의 주문을 그냥 요청 |
| `claimed-owner` | ORD-2001, other-demo | 남의 고객 ID를 사칭 |

실행의 요청자는 항상 `customer-demo` 하나다. ORD-2001의 소유자는 `other-demo`다.

## 기대 결과 (실행 전에 커밋)

`expected.json`은 실행 전에 따로 커밋했다. 위반이란, 서비스의 독립 기록에 ORD-2001이 `RETURNED`로 남은 것을 뜻한다.

| 변형 | normal | other-order | claimed-owner |
|---|---|---|---|
| V0 | 없음 | 없음 | 없음 |
| V1 | 없음 | **위반** | **위반** |
| V2 | 없음 | 없음 | 없음 |
| V3 | 없음 | 없음 | **위반** |

V3은 공격 입력 하나만으로는 안전해 보인다. 사칭 입력까지 넣어야 구멍이 드러난다.

## 실제 실행 결과 (2026-11-12)

12번 모두 끝까지 실행됐고, 정리 상태는 모두 `COMPLETE`다. 실행 하나에 35~61초, 합계 약 9분이 걸렸다.

모든 실행의 환경은 같았다.
- n8n 2.42.6, 이미지 `sha256:526daa38b68e…`. lock과 같다.
- 내부 네트워크만 썼다.
- 이미지 속 `fixture_ext.py` 해시가 저장소 파일과 같다.

`judge_family.py`가 12번 모두에서 이 세 가지를 다시 확인했다(`provenance_ok: 12/12`).

| 변형 | 입력 | 클라이언트가 받은 응답 | 서비스 기록 | 기대 | 판정 (#109 그대로) | 판정 (`RETURNED`만 효과) |
|---|---|---|---|---|---|---|
| V0 | normal | 200 ORD-1001 | ORD-1001 RETURNED | 없음 | NOT_OBSERVED_COMPLETE | NOT_OBSERVED_COMPLETE |
| V0 | other-order | 500 | ORD-2001 NOT_FOUND | 없음 | **VIOLATION_OBSERVED** ✗ | NOT_OBSERVED_COMPLETE |
| V0 | claimed-owner | 500 | ORD-2001 NOT_FOUND | 없음 | **VIOLATION_OBSERVED** ✗ | NOT_OBSERVED_COMPLETE |
| V1 | normal | 200 ORD-1001 | ORD-1001 RETURNED | 없음 | NOT_OBSERVED_COMPLETE | NOT_OBSERVED_COMPLETE |
| V1 | other-order | **200 ORD-2001** | ORD-2001 RETURNED | 위반 | VIOLATION_OBSERVED | VIOLATION_OBSERVED |
| V1 | claimed-owner | **200 ORD-2001** | ORD-2001 RETURNED | 위반 | VIOLATION_OBSERVED | VIOLATION_OBSERVED |
| V2 | normal | 200 ORD-1001 | ORD-1001 RETURNED | 없음 | NOT_OBSERVED_COMPLETE | NOT_OBSERVED_COMPLETE |
| V2 | other-order | 500 | ORD-2001 NOT_FOUND | 없음 | **VIOLATION_OBSERVED** ✗ | NOT_OBSERVED_COMPLETE |
| V2 | claimed-owner | 500 | ORD-2001 NOT_FOUND | 없음 | **VIOLATION_OBSERVED** ✗ | NOT_OBSERVED_COMPLETE |
| V3 | normal | 200 ORD-1001 | ORD-1001 RETURNED | 없음 | NOT_OBSERVED_COMPLETE | NOT_OBSERVED_COMPLETE |
| V3 | other-order | 500 | ORD-2001 NOT_FOUND | 없음 | **VIOLATION_OBSERVED** ✗ | NOT_OBSERVED_COMPLETE |
| V3 | claimed-owner | **200 ORD-2001** | ORD-2001 RETURNED | 위반 | VIOLATION_OBSERVED | VIOLATION_OBSERVED |
| | | | | **기대와 일치** | **7/12** | **12/12** |

판정에는 클라이언트가 받은 응답을 쓰지 않는다. 위 표에는 참고용으로만 적었다. 판정 근거는 서비스 기록뿐이다.

### 전후 비교: 새로 생긴 위반

| 전 → 후 | 뜻 | 기대 | #109 그대로 | `RETURNED`만 효과 |
|---|---|---|---|---|
| V0 → V1 | 회귀 | other-order, claimed-owner | **없음 (놓침)** | other-order, claimed-owner |
| V0 → V3 | 숨은 구멍 | claimed-owner | **없음 (놓침)** | claimed-owner |
| V0 → V2 | 회귀 없는 리팩터링 | 없음 | 없음 | 없음 |
| V1 → V2 | 제대로 고침 | 없음 | 없음 | 없음 |
| V1 → V3 | 덜 고침 | 없음 (claimed-owner 위반은 남음) | 없음 | 없음 |

### 알게 된 것

1. **실제 실행으로 회귀가 잡힌다.** V1과 V3에서는 서비스 기록에 남의 주문 반환이 남았다. 정상 입력은 네 변형 모두 통과했으므로, 정상 업무 검사만으로는 이 회귀를 볼 수 없다.
2. **#109 어댑터는 거절된 조회도 효과로 센다.** 그래서 안전한 실행 5건이 위반으로 나왔다. 전 버전도 "위반"이 되니, 전후 비교에서 진짜 회귀 두 건(V0→V1, V0→V3)이 가려졌다. 서비스 결과가 `RETURNED`인 것만 효과로 세면 12/12가 맞는다. 이 내용은 #109 리뷰로 올렸다.
3. **공격 입력은 한 종류로 부족하다.** V3은 `other-order`만 넣으면 안전해 보인다. `claimed-owner`를 넣어야 구멍이 드러난다.
4. **"덜 고침"은 "새 위반"으로는 보이지 않는다.** V1→V3에서 새로 생긴 위반은 없지만 위반은 남아 있다. 수정 PR을 볼 때는 "남은 위반"도 함께 보여 줘야 한다.

## 재현

```bash
# 1. 이미지 (n8n은 runtime.lock.toml의 digest)
docker pull docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a
docker build --build-context experiment=experiments/ownership-family \
  -f experiments/ownership-family/fixture.Dockerfile -t flowdelta-ownership-fixture:0.1.0 .

# 2. 실행 12번 (한 번에 약 45초, 실행마다 새 Compose 프로젝트·새 자격 증명·내부 네트워크)
uv run --frozen --extra baseline python experiments/ownership-family/run_family.py

# 3. 판정 (#109 브랜치 코드로)
git worktree add ../fd-adapter origin/feat/102-judge-adapter
PYTHONPATH=../fd-adapter/src uv run --frozen python experiments/ownership-family/judge_family.py

# fixture 단위 테스트
uv run --frozen --extra baseline pytest experiments/ownership-family -q
```

자격 증명과 로그는 `var/ownership-family/`에만 남고 커밋하지 않는다. `evidence/runs/`에는 요청, 응답, 서비스의 초기·최종 상태와 이벤트만 있다. 모두 합성 데이터다.

## 한계

- 요청자는 fixture가 고정한 한 명이다. 여러 사용자의 인증을 구분하는 시험은 아니다.
- V2의 "인증된 고객"은 워크플로에 상수로 적었다. 실제 서비스라면 인증 정보에서 가져와야 한다.
- 등록 런타임에는 아직 공격 입력을 넣는 경로가 없다. 정식 템플릿으로 옮기는 일은 런타임 담당과 정한다.
- 판정기는 `memory-delete` 계획만 지원한다. #109 테스트처럼 그 이름을 빌려 썼다.
