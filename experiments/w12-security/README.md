# 12주차 보안 점검 — 구현된 범위의 실제 점검

보안 점검 리포트(docs `deliverables/w12-보안점검리포트/`)의 증거와 재현 방법이다. 웹 서버(#91~#96)는 아직 코드가 없어서 여기에 없다. 코드가 들어오면 같은 방식으로 추가한다.

## 재현

```bash
# 1) 합성 주문 서비스와 사례 입력 처리에 공격 요청 (팀 코드만 실행)
uv run --frozen --extra baseline python experiments/w12-security/probe.py <out.json>

# 2) 비밀값: 두 저장소 전체 git 이력
docker run --rm -v <repo>:/repo:ro trufflesecurity/trufflehog:3.99.8 git file:///repo --no-verification --json --no-update

# 3) 의존성 취약점
uv export --frozen --all-extras --no-hashes --no-emit-project --format requirements-txt -o req.txt
uvx --from pip-audit==2.9.0 pip-audit -r req.txt --no-deps --disable-pip

# 4) 정적 분석
uvx --from semgrep==1.181.0 semgrep scan --config p/python --config p/security-audit --metrics off src tools runtime
```

## 결과 (2026-11-12)

| 점검 | 결과 | 증거 |
|---|---|---|
| 합성 서비스 공격 12개 | 수정 전 11/12, **수정 후 12/12 막힘**. `/openapi.json`이 인증 없이 열려 있던 것을 막음 | `evidence/probe-before-fix.json`, `evidence/probe-after-fix.json` |
| 사례 입력 공격 8개 | 8/8 막힘 (경로 조작, 절대 경로, 변조, 중복 키, NaN, 8 MiB 초과, 디렉터리 junction) | 같은 파일 |
| 비밀값 | platform 0건. docs 18건은 모두 9월 사전 조사 때 로컬 Juice Shop이 발급한 연습용 JWT. 실제 서비스 자격 증명 0건 | `evidence/scan-summary.json` (값은 옮기지 않음) |
| 의존성 | 38개 패키지, 알려진 취약점 0건 | 같은 파일 |
| 정적 분석 | 222개 규칙, 135개 파일, 3건. 모두 오탐(매개변수 바인딩, 고정 문자열, 개발용 도구의 고정 URL) | 같은 파일 |
