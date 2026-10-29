# Workflow-only 고정 profile

[#48](https://github.com/serendipity-sec/platform/issues/48)의 입력 단계입니다.
일반 `load_case`의 manifest 검사는 파일 자체의 무결성을 확인합니다. 이 기능은
별도로 고정한 profile과 대조하여, manifest를 다시 계산한 환경·정책·모델 변경도 거부합니다.

## 사용

저장소 루트에서 `var`를 준비합니다. profile은 검토한 기준 입력에서 한 번만 생성합니다.
후보마다 다시 생성하면 비교 기준도 바뀌므로 검증이 되지 않습니다.

```powershell
New-Item -ItemType Directory -Force var | Out-Null
uv run --frozen flowdelta freeze-profile examples/order-lookup --profile-id order-lookup --revision 1.0.0 --out var/order-profile.json
```

출력의 `profile_sha256`은 정규 JSON 직렬화의 SHA-256입니다. profile ID·revision과 함께
신뢰하는 설정/리뷰 기록에 보관합니다. 후보가 제공하는 새 해시로 대체하지 않습니다.
파일 위치는 후보 bundle 바깥이며 `profile_format=flowdelta-fixed-profile-v1`과
기존 `schema_version=0.4.0`을 별도로 관리합니다. 기존 Handoff 필드는 바꾸지 않습니다.

```powershell
uv run --frozen flowdelta check-profile examples/order-lookup --profile var/order-profile.json --profile-sha256 <보관한-profile_sha256>
uv run --frozen flowdelta profile-snapshot examples/order-lookup --profile var/order-profile.json --profile-sha256 <보관한-profile_sha256> --out var/order-profiled-handoff
uv run --frozen flowdelta analyze var/order-profiled-handoff --out var/order-analysis.json
```

`profile-snapshot`은 같은 메모리의 원문 bytes를 검사하고 인계 파일을 작성합니다.
검사 후 입력을 다시 읽지 않습니다. 검증 결과는 profile ID/revision/hash,
case ID, 전후 snapshot ID를 포함하며 stdout과 Handoff의
`snapshots.extensions["flowdelta.fixed_profile"]`에 함께 보존됩니다.
이 기록은 서명이 아니므로 소비자는 별도로 신뢰한 profile hash와 대조해야 합니다.
실패하면 exit 2와 입력값을 포함하지 않는 오류 코드를 내고 handoff를 쓰지 않습니다.
기존 파일/디렉터리는 덮어쓰지 않습니다.

## 고정 범위

| 고정하는 값 | 검사 방식 |
|---|---|
| 기준 workflow·업무·정책·환경·초기 상태 | 원문 파일의 ID/경로/크기/SHA-256 |
| 의존성 파일 전체·버전·requires·전후 root | 전체 manifest와 각 파일을 고정; 전후 root가 같아야 함 |
| 사례 family·역할 경로·obligation·확장 선언 | case ID와 provenance_ref를 제외한 case 계약 해시 |
| 모델 노드 | langchain `lm*`/`embeddings*`/`reranker*`, 직접 OpenAI·Anthropic·Gemini·Ollama 노드, `ai_languageModel`/`ai_embedding`/`ai_reranker` 공급 노드; position만 제외 |
| 모델 연결 | 모델 연결의 양쪽 노드·포트·인덱스 |
| 추가로 고정할 설정 노드 | `freeze-profile --fixed-node <node-id>`를 반복 지정 |

후보 workflow, candidate case ID와 출처 문서는 바꿀 수 있습니다. baseline의 보안 상태는
계속 `UNKNOWN`입니다. 의존성 manifest가 없거나 선언된 하위 workflow가 해소되지 않으면 거부합니다.
동적 모델 설정과 추가 고정 노드의 동적 parameters(`=...`, `{{...}}`)도 고정값으로 인정하지 않습니다.
현재 계약은 MCP client endpoint와 MCP_DESCRIPTOR의 연결을 증명하지 못하므로
MCP client가 있으면 `PROFILE_MCP_DEPENDENCY_UNSUPPORTED`로 거부합니다.
descriptor를 선언했다는 이유만으로 MCP를 고정했다고 판정하지 않습니다.

원문 bytes 고정이므로 고정 파일의 공백이나 경로 변경도 새 profile revision이 필요합니다.
모델 노드 목록을 비교해 모델 추가·삭제·유형 변경도 확인합니다. 사용자 정의 HTTP/code 노드 안에서
임의로 모델을 부르는 코드를 탐지하는 기능은 아닙니다. 그런 구성은 별도 고정 노드/환경/의존성으로
명시하고 소비 담당자가 검토해야 합니다. 외부 제공자의 모델 별칭이나 실제 원격 환경은 이 검사만으로
불변임을 증명할 수 없습니다.

## API와 검증 경계

- `freeze_profile(loaded, profile_id=..., revision=...)`: 명시적인 기준 생성.
- `read_profile(path, expected_sha256=...)`: 신뢰하는 해시와 profile의 동일성 확인.
- `write_profile(profile, output, input_root=...)`: 입력 밖에만 저장. 임시 파일의 write/flush/fsync
  후 원자적으로 공개하며 기존 출력은 덮어쓰지 않습니다. Windows는 디렉터리 fsync를 제공하지 않습니다.
- `validate_profile(loaded, profile)`: 이미 신뢰하는 profile과 입력 비교.
- `load_profiled_handoff(bundle, profile_path, expected_sha256=...)`: 파일 경계 검사부터 Handoff까지.

`validate_profile`의 메모리 인수는 검증된 로더의 결과여야 합니다. 후보가 넘긴 임의의 Python
객체를 신뢰하는 API가 아닙니다. `FIXED_PROFILE_MATCH`는 선언된 구성의 일치이며 실행 승인,
보안 판정, AI 노드 지원, 실제 A/B/C 실행 성공을 뜻하지 않습니다. 기존 일반 `snapshot`은
기존 범용 동작을 유지하므로 MVP 통합 경로는 반드시 `profile-snapshot` 또는 위 API를 사용해야 합니다.

정책 binding·AI 연결 의미·C 검사 계약·공용 DB·선정 사례의 실제 agent 실행은 별도 기능입니다.
현재 테스트는 두 기존 정상 업무의 입력을 검사하고 실제 A 함수를 통해 분석까지 소비합니다.
선정한 n8n-claw 사례 전체를 지원한다는 뜻은 아닙니다.
