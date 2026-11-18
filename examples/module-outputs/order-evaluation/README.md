# 주문 평가 통합 — 실제 n8n 실행 영수증

2026-11-17, #119의 **순수 합성 개발·통합 계열** 실행이다. 독립 평가셋의 탐지 정확도는 아니다.

| 확인 | 결과 |
|---|---|
| 실제 n8n 2.42.6 실행 | 6쌍 × 3입력 × 전후 = 36회, 실행·수집·정리 완료 |
| 정상 업무 | 12건 모두 성공 |
| 새 위반 | 시험별 3건 |
| 시험 범위에서 수정 효과 | 시험별 3건 |
| 수정 후 남은 위반 | 사칭 입력에서 1건 |
| 관측한 위반 없음 | 정상 입력·정상 변경을 포함한 11건 |
| 전체 pytest | 887 passed, 54 skipped |
| CLI·표현식 마무리 보완 후 관련 pytest | 66 passed |

표의 비교 결과 18건은 **각 비교쌍의 세 시험**을 센 수치다. p04의 일반 타인 주문 입력에서는
수정 효과를 보이지만 사칭에서는 위반이 남는다. 독립 계열 18개나 전체 수정 완료로 해석하지 않는다.

`evidence-20261117.zip`은 18개 최종 공통 bundle, 원시 독립 이벤트·상태·응답, A/B 산출물,
판정 보고서, 전체 요약을 담는다. 실행 자격·DB·private 로그는 제외했고 해당 실행의 실제
일회용 비밀값이 공유 파일에 없는지 대조했다. `archive-index.json`은 압축한 원본 bytes의
SHA-256 목록이다. 압축 파일의 해시와 검증 범위는 [receipt.json](receipt.json)에 있다.

실행 source/config snapshot은 `c5cf8fd`의 해당 bytes와 일치한다. 이후 `5c05f57`
보완 커밋은 상대 경로 재판정·누락 파일 오류 처리와 보수적인 문자열 문법 제한이다.
정확한 SHA는 receipt의 `followup_commit`에서 읽는다. 등록된 6개 A 분석은 보완 전후
bytes가 같았고, 36개 저장 증거의 판정도 동일했다. receipt는 암호학적인 실행 attestation이 아니다.

## Docker 없이 저장 결과 재검증

저장소 루트에서 새 출력 경로로 푼다. 아래 명령은 기록된 증거를 재판정하며 n8n을 새로 실행하지 않는다.

```powershell
uv run --frozen --extra baseline python -m zipfile -e examples/module-outputs/order-evaluation/evidence-20261117.zip var/archived-order
uv run --frozen --extra baseline python tools/check_order_evaluation.py var/archived-order
uv run --frozen --extra baseline flowdelta check-pipeline var/archived-order/p0/s1/b
```

새 실제 실행은 [ORDER_EVALUATION.md](../../../docs/ORDER_EVALUATION.md)를 따른다.
54개 skip은 PostgreSQL 미설정과 선택 Semgrep 미설치 항목이다. pytest 경고 2개도 원본 로그에
남아 있다. 이번 영수증으로 외부 DB 검사나 범용 workflow 지원을 완료했다고 주장하지 않는다.
