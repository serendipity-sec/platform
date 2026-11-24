# 평가 집계 — 원시 시도 기록에서 표 다시 만들기

Refs docs #56 (#67 3-3), docs #70 6절, platform #128. 평가 결과를 보기 전에 집계 규칙을 코드로 고정해 둔다.
규칙의 근거와 나머지 사전 설정은 docs `research/2026-11-12-56-평가-사전설정-초안.md`에 있다.

```bash
uv run --frozen --extra baseline pytest experiments/eval-aggregate     # 규칙 시험 24개
python experiments/eval-aggregate/aggregate.py --attempts <attempts.json> --truth <truth.json> --out <report.json>
```

- 표준 라이브러리만 쓴다. 같은 입력 바이트면 같은 출력이 나온다. 출력에 입력·스크립트의 SHA-256을 남긴다.
  스크립트 해시는 줄바꿈을 LF로 맞춘 바이트 기준이다.
- `truth.json`은 모든 라벨러가 라벨을 낸 뒤 봉인을 풀어 만든다(`truth_from_labels()`, docs #70).
- `flowdelta run-evaluation-attempt`(#125)의 `attempts.json`을 그대로 넣을 수 있다. `status`·`runs`가 없는
  손 작성 기록은 판정 라벨만으로 센다.

## 고정한 규칙

| 항목 | 규칙 |
|---|---|
| 회귀 확인 | after가 `VIOLATION_OBSERVED`이고 before가 `NOT_OBSERVED_COMPLETE`·`BLOCKED`일 때만. 양쪽 다 위반이면 기존 위험이지 새 회귀가 아니다 |
| 판단 불가 | `INCONCLUSIVE`·`UNSUPPORTED`·한쪽 누락은 검출로도 안전으로도 세지 않는다. 분모에 남기고 따로 보고한다 |
| 실행 상태 | 판정 라벨은 `COMPLETE`·`INCOMPLETE` 기록에서만 읽는다. `NOT_SELECTED`(방법이 고르지 않음)는 미검출, `FAILED`·`NOT_RUN`·`INTERRUPTED`·`PREPARED`는 "실행 안 됨", `UNSUPPORTED`는 따로 센다. 모든 기록이 `UNSUPPORTED`인 방법은 `unsupported_arms`에만 적고 비교하지 않는다 |
| 정답 | 사람 라벨은 before·after가 모두 있어야 한다. 누락·중복·모르는 값은 오류. before가 `근거 부족`이면 after 위반이 있어도 회귀로 확정하지 않는다(`INCONCLUSIVE`) |
| 기록 무결성 | 같은 (사례, 방법, 시도) 기록이 둘이면 오류. 하나의 실행(`run_id`)을 두 기록이 쓰면 오류. 비교하는 방법끼리 사례마다 시도 번호가 같아야 한다 |
| 사례 검출 | 예산(시도 횟수) 안에서 한 번이라도 확인하면 검출 |
| 탐지율·오경보율 | 사례(`변경쌍/시나리오`) 단위. Wilson 95% 구간을 같이 낸다 |
| 방법 비교 | **변경쌍 단위**로 짝지어 비교한다. 한 변경쌍의 시나리오들은 같은 workflow를 공유해 독립 표본이 아니다. McNemar 정확 검정(쌍별 검출 여부)과 Wilcoxon 부호순위 정확 검정(쌍별 평균 확인 비율). Holm 보정, 유의수준 0.05 |
| 예산 | 변경쌍·시도마다 실행한 시나리오 수(`NOT_SELECTED`·`UNSUPPORTED` 제외)를 방법별로 낸다. 두 방법의 최댓값이 다르면 "예산이 달라 방법 차이로 해석하지 않음" |
| 결과 문구 | 보정 후 p < 0.05가 아니면 "이 규모에서 차이를 확인하지 못함". "차이가 없다"고 쓰지 않는다 |

**변경쌍 수가 적으면 어떤 결과도 유의할 수 없다.** 양측 정확 검정은 McNemar는 엇갈린 변경쌍이 6개 이상,
Wilcoxon은 0이 아닌 차이가 6개 이상이어야 p < 0.05가 가능하다(5개면 최소 p = 0.0625).
비교를 두 개 이상 하면 Holm 보정 때문에 더 많이 필요하다. 그보다 작은 평가는 기술 통계로만 보고한다.
