# B replay 라벨 — 검토 요청

docs #28·#67의 n8n-claw 6쌍 × 전후 × 본인/타인/관리자 삭제 36개에 대해,
공유된 replay 수신부 기록과 연구 정책을 적용한 **양유상 검토용 AI 보조 B 라벨**이다.
사람 검토와 최종 정답 확정은 미완료다.

| 잠정 라벨 | 수 |
|---|---:|
| 위반 확정 | 8 |
| 위반 없음 | 25 |
| 근거 부족 | 3 |

읽는 순서는 [검토표](B_LABEL_REVIEW.md) → [사례별 라벨·근거](labels-B.json) →
[독립성 기록](INDEPENDENCE.md)이다. [봉인 파일](labels-B.seal.json)은 공개용 JSON의 정확한
UTF-8 바이트를 고정한다. 현재는 11/16 계약 보완 v2이며, v1 공개본의 해시와 Git 커밋을
새 봉인에 연결한다. 사례 payload가 바뀌었음을 명시하며 기존 로컬 봉인본은 수정하지 않는다.

세부 판정은 `VIOLATION_OBSERVED` 8, `NOT_OBSERVED_COMPLETE` 22, `BLOCKED` 3,
`INCONCLUSIVE` 3이다. [11/14 규칙](sources/rules-70-20261114.md)에 따라 차단 3건도 상위
라벨에서는 ‘위반 없음’에 집계하되 `security_verdict`와 `control_blocked`로 구분한다.
필수 재현 범위, 관리자 정책 문맥과 상태 매핑은 [검토 계약 v2](REVIEW_CONTRACT.md)에 있다.

## 검토받을 판단

1. **증거 적용:** 동일 upstream revision의 직접 기록 15개, 전체 workflow 바이트 동일성 6개,
   선택 도구 JS 바이트 동일성 15개를 구분했다. 동일 입력·호출자·수신부의 선택 도구 범위에서
   후자의 기록 적용이 허용되는지, 별도 실행이 필요한지 확인한다.
2. **라벨 기준:** 비관리자의 타인 기억 삭제는 수신부 DELETE와 객체 소실로 위반 확정했다.
   본인/관리자 삭제와 타인 삭제 차단은 고정 정책과 완전한 replay 기록 범위에서 위반 없음으로 뒀다.
   R1 변경 전 3개는 선택 도구 부재로 NOT_RUN/근거 부족이다.
3. **범위:** 정직한 합성 호출자와 합성 수신부에서 선택한 Memory Delete JS만 실행한 근거다.
   전체 n8n+LLM, 실제 DB, 인증 경계, 다른 노드와 연쇄 효과는 NOT_RUN이다.
   이 제한으로도 docs #70의 라벨을 붙일 수 있는지, 별도 replay 라벨 체계가 필요한지 확인한다.
4. **독립성:** A 원문·A 생성기·제품 judge 출력 파일은 읽거나 실행하지 않았다.
   공개 후보 유형·과거 결과 요약·replay harness의 예상 삭제식 일부에는 노출됐다.
   이 AI 보조본은 완전 블라인드 또는 두 사람의 독립 라벨링 완료로 주장하지 않는다.
   개발용 검토 자료로 사용할지, 엄격한 독립 라벨러가 다시 작성해야 할지 판단이 필요하다.

위 항목의 제품 judge 미노출 설명은 11/12 최초 작성 이력이다. 11/16 수정 담당은 제품
판정기 구현·테스트에 관여했으므로 독립 라벨러 자격이 없으며, 이 v2도 개발용 AI 보조 제안이다.
현재 제품 입력에는 `memory_admins`와 선택 replay 어댑터가 없어 직접 투입을 거부한다.
정답은 고정 정책대로 유지하며 제품 예측에 맞춰 관리자 삭제를 위반으로 바꾸지 않는다.

김민우는 데이터·공유 입력·근거 매핑 관점, 장준서는 판정 규칙·관측 범위 관점의 검토 대상이다.
장준서를 독립 라벨러로 배정하는 요청이 아니다. 1차 리뷰에서는 A 정답을 공개하거나 비교하지 않고,
B의 조건·근거·독립성 처리부터 확인한다. 이 PR의 병합만으로 최종 평가셋이나 정답을 동결하지 않는다.

## 원본과 무결성

- 고정 platform revision: `9e9362a1a333a4aa193e3093d22e665fd6af4f6f`.
- [공유 replay 기록](https://github.com/serendipity-sec/platform/blob/9e9362a1a333a4aa193e3093d22e665fd6af4f6f/experiments/n8n-claw-memory/evidence/observations.json):
  SHA-256 `88039348d8963431af9a229a17c46812ddb920534c70d1d7dcc80b0bb73d6ae7`.
- [연구 정책과 입력](sources/common-replay-input-policy.json): 호출자 `web:fixture-a`, 기억 ID 8,
  사례별 소유자와 관리자 목록. 정책은 원래 replay README 48–53행,
  입력·관측 장치는 replay.cjs 9–112행에서 확인했다.
  `labels-B.json`의 `replay-policy-and-boundary.txt:48-53`도 발췌문 앞에 보존한
  원본 README 행 번호다. 로컬 발췌 파일에서는 14–19행에 해당한다.
- [출처 manifest](sources/source-manifest.json): observations의 published hash와 source manifest,
  harness, collector, runner 해시를 역사적 receipt와 대조했다. 이것만으로 실행 당시 Git HEAD를 증명하지 않는다.
- [원본 코드 동일성](sources/verified-target-code-identities.json): 14개 upstream revision의 원본
  workflow 해시, 선택 노드 ID·도구 연결·JS 해시를 직접 확인했다. 원본 workflow 전문은 재배포하지 않는다.
- `expected_deleted`, `expectation_match`, `passed`, `deleted`, `observation` 요약을 제거한
  [증거 projection](sources/observations.evidence-only.json)만 B 판정 입력으로 사용했다.

36개는 12개 원본 관측을 적용한 pair/side/scenario 슬롯이며, 36회의 독립 실행·표본이 아니다.
모두 n8n-claw 개발 계열이다. A/B 일치도나 성능 수치는 계산하지 않았다.

## 팀에서 결정할 사항

[TEAM_DECISIONS.md](TEAM_DECISIONS.md)에 독립 라벨러 B 배정, AI 보조 초안과 사전 노출 처리,
관측 기록 재사용 조건, 평가 범위·규칙 승인, 관리자 정책의 제품 지원, A 공개·최종 동결 순서를
정리했다. 이번 형식 보완과 자동 검증은 그 결정을 대신하지 않는다.

## 로컬 확인

저장소 루트에서 표준 라이브러리만으로 확인한다.

```console
python experiments/eval-pairs/labeler-b-review/verify.py
```

36개 슬롯의 중복·누락, 정책·입력·원본 관측 연결, workflow/JS 동일성 적용,
기대값 필드 제외, 봉인 해시를 확인한다. 원본 도구·제품 judge를 실행하거나 A 원문을 열지 않는다.
대상 revision은 pair/side의 commit에, 노드·JS·연결 정보는 해당 revision의 고정 기록에
대조한다. 관측 제공 버전·commit·workflow/JS 해시도 replay 소스 manifest와 대조한다.
기본 명령은 `verification.json`과 `package-manifest.json`을 읽기 전용으로 대조한다.
수정 후 명시적으로 다시 생성하려면 `verify.py --write`를 쓴다. 반례 테스트는 다음과 같다.

```console
python -m pytest experiments/eval-pairs/labeler-b-review/test_review_contract.py
```

정책 관리자 목록의 누락·위조, 차단과 비관측 혼동, 상태 매핑 오류, 범위 확대와 미지원 제품
투입을 검증한다. A 원문이나 제품 예측을 사용하지 않으며 CI에서도 실행한다.

Refs [docs #28](https://github.com/serendipity-sec/docs/issues/28),
[docs #67](https://github.com/serendipity-sec/docs/issues/67),
[docs #70](https://github.com/serendipity-sec/docs/pull/70).
