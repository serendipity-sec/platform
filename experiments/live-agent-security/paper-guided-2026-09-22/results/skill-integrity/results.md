# Skill 전체 패키지 무결성 계약 결과

이 결과는 합성 패키지와 결정론적 Ed25519 lab key에 한정된다. 서명은 파일 집합·버전·발행자 판단의 입력이며 안전성 판정 자체가 아니다.

| 사례 | entrypoint만 검사 | 전체 트리 | 서명 | 신뢰 발행자 | 버전 | 안전 정책 | 최종 결정 |
|---|---:|---:|---:|---:|---:|---:|---|
| I00-valid-clean | PASS | PASS | PASS | PASS | PASS | PASS | `ALLOW` |
| I01-skill-edited | FAIL | FAIL | PASS | PASS | PASS | PASS | `HOLD_INTEGRITY` |
| I02-helper-edited | PASS | FAIL | PASS | PASS | PASS | PASS | `HOLD_INTEGRITY` |
| I03-unsigned-file-added | PASS | FAIL | PASS | PASS | PASS | PASS | `HOLD_INTEGRITY` |
| I04-signed-file-removed | PASS | FAIL | PASS | PASS | PASS | PASS | `HOLD_INTEGRITY` |
| I05-untrusted-signer | PASS | PASS | PASS | FAIL | PASS | PASS | `HOLD_UNTRUSTED_SIGNER` |
| I06-old-version-replay | PASS | PASS | PASS | PASS | FAIL | PASS | `HOLD_REPLAY` |
| I07-validly-signed-malicious | PASS | PASS | PASS | PASS | PASS | FAIL | `DENY_SECURITY` |

## 직접 확인된 계약

- helper 수정, 파일 추가, 파일 삭제는 `SKILL.md` digest만 보면 통과하지만 full-tree closure는 모두 보류한다.
- 암호학적으로 유효한 서명도 신뢰하지 않는 발행자와 구버전 replay를 별도로 막아야 한다.
- 유효하게 서명된 악성 패키지는 integrity와 authenticity를 통과하므로 안전 정책에서 별도로 거부해야 한다.
