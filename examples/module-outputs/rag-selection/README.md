# #121 A·RAG 선택 → 등록 C 실행 재현

2026-11-17, 순수 합성 개발 사례 order-eval-p03의 통합 검증이다. 독립 평가셋의 탐지율이나 RAG의
추가 검출 효과를 입증한 실험은 아니다. 설정 없이 실행하는 기존 정상 데모도 별도로 검증했다.

- 실제 BGE-M3 1024차원 검색 → 실제 Qwen3-8B 제안 → 계약·예산 검사 → 실제 n8n 2.42.6 실행.
- normal, other-order, claimed-owner와 추가 claimed-customer 선택. claimed-tenant는 예산으로 제외.
- 네 시험 × 전후 = 8회 모두 실행·증거 수집·정리 완료. 독립 서비스 증거로 다시 판정했다.
- 네 계획을 PostgreSQL에 저장·ID 조회·내보내고 C 소비 plan bytes 및 hash와 비교해 일치했다.
- 기존 정상 주문 데모 전후 2회 성공, 정상 업무 검사·독립 판정·bundle 검사 통과.
- Windows 전체 985 passed / 3 skipped. 추가 평가 RAG→C 계약 시험 1 passed.
  Windows에서 생략된 실제 Semgrep 시험 3개는 기존 Linux 이미지에서 3 passed.
- Ruff check/format, Mypy(105 source), 입력·계약 예제 두 개씩과 sdist/wheel 빌드 통과.

전체 Windows 검사에서 기존 static-report 테스트의 escape SyntaxWarning 2개와 자식 Python 출력
encoding 경고 1개가 있었다. encoding 사례는 CI와 같은 PYTHONUTF8=1로 다시 실행해 통과했다.

모델은 기존 PC 파일을 읽기 전용으로 사용했고 가중치·서버를 배포하지 않는다. 실제 서버는
llama.cpp b12030의 OpenAI 호환 API다. Ollama native adapter는 로컬 HTTP double로 검사했다.
외부 무료 API에는 호출하지 않았다. endpoint/model/key 환경변수를 지정해 같은 인터페이스를 쓴다.

CPU cold 호출은 600초 상한으로 실패했고 다음 pilot 응답은 후보를 중복 반환해 C 전에 거부됐다.
후보 enum·개수 상한과 유일성 검사를 적용한 성공 pilot은 두 후보를 제안했다. 성공 생성 호출은
108,368ms, 입력 6,628 tokens(캐시 6,595), 출력 232 tokens였다. 실패 시도는 성공 통계에 섞지 않는다.
자동 재시도는 없다. 느린 로컬 모델의 응답 시간 한계와 무료 API의 quota는 실제 실행 환경에 따른다.

## 증거 읽기

evidence-20261117.zip에는 validation-receipt.json(소스·모델 hash), db-round-trip.json,
성공 selection/호출 영수증, 네 C 최종 bundle/판정, 실패 pilot 영수증과 정상 데모 요약을 담았다.
private 자격·DB URL·materialized workflow·서버 원본 로그는 포함하지 않는다. 모델 가중치 bytes의
SHA-256은 실제 파일을 다시 읽어 확인했다. 압축 파일 자체의 hash는 receipt.json에 있다.

압축을 새 폴더에 풀고 같은 등록 자료가 있는 저장소에서 저장 bundle을 재판정할 수 있다.

```powershell
uv sync --frozen --extra baseline --extra knowledge
uv run --frozen --extra baseline --extra knowledge flowdelta judge-order-evaluation-bundle <압축해제>/s0/bundle --out var/rejudge-rag-s0
```

s1/s2/s3도 같은 방식이다. DB export는 B 산출물과 등록 scenario.json을 복원한다. 새 C 실행에는
그 계획과 일치하는 기존 input handoff·analysis·catalog를 함께 module bundle로 묶어야 한다.
새 실행과 평가 입출력 설정은 [RAG 안내](../../../docs/RAG_PLANNING.md)를 따른다.
