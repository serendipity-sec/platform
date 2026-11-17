# A·RAG 시험 계획

Refs #121. 공통 runner의 고정 공유 검색과 등록 주문 시험 선택을 제공한다.

`run-pipeline ... --planning-config config.json`으로 공유 검색을 선택한다. 기존 옵션은 그대로
사용한다. VECTOR 모드에서는 `--cards` 디렉터리를 읽지 않으며, DB의 승인·색인된 카드를 읽는다.

```json
{
  "schema_version": "0.4.0",
  "config_format": "flowdelta-planning-config-v1",
  "retrieval": "VECTOR",
  "embedding_profile_id": "고정한 profile_id",
  "search_limit": 50,
  "frozen_cards": [
    {"card_id": "카드 ID", "version": "1.0.0", "sha256": "canonical 카드 bytes의 SHA-256"}
  ]
}
```

DB 연결과 임베딩 endpoint는 기존 `FLOWDELTA_KB_DATABASE_URL`, `FLOWDELTA_EMBEDDING_*` 설정을
사용한다. 설치·색인은 [임베딩 안내](EMBEDDING.md)를 따른다. runner가 DB migration·카드 색인·모델
다운로드를 자동 수행하지 않는다. 고정 카드 누락·해시 변경·profile 불일치는 실패다.

검색은 고정 카드 집합을 **순위 계산 전에** 제한한다. 반환 KB snapshot·검색 근거·profile·query
해시가 plan과 bundle에 연결된다. 요약에는 실제 생성 모델 호출이 없음을 남긴다. 빈 검색은 기존
정상 시험 fallback과 함께 `retrieval_empty=true`로 보존하며 검색·DB 장애와 구분한다.

임베딩 QUERY에는 변경 값, 노드 종류, 업무 목적·성공 조건·보호 자원, 정책 조건과 A 경로의 지원
상태·한계를 전달한다. case 이름·정답·기대 판정·analysis/snapshot ID와 해시는 전송하지 않는다.
기존 민감 필드명 redaction은 heuristic이다. 실제 원문 전송 범위는 이 필드들이며, 검색 endpoint는
호출자가 신뢰하는 서버로 설정한다. 추적 ID는 로컬 검색 산출물에 보존한다.

## 생성 모델로 등록 시험 선택

`evaluate-order-family --manifest runtime/order-evaluation/family.json --out var/rag-001
--planning-config config.json --execute`를 사용한다. `--execute`가 없으면 계획까지만 만든다.
일반 `run-pipeline`은 단일 계획 경로이므로 generation 설정을 거부한다. 다중 시험과 실제 C 소비는
등록 주문 runner가 담당한다. 기존 `evaluate-order-family`는 설정이 없으면 기존 세 시험을 실행한다.

설정에 다음 항목을 추가한다. 모델은 자동 설치하지 않는다. endpoint/model은 실행 환경에서 고른다.

```json
{
  "generation": {
    "provider": "ollama",
    "base_url": "http://127.0.0.1:11434",
    "model": "실행 가능한 로컬 모델",
    "response_format": "json_schema",
    "timeout_seconds": 120,
    "max_output_tokens": 2048
  },
  "budget": {"max_test_pairs": 4, "max_c_runs": 8, "max_runtime_seconds": 1800}
}
```

외부 API는 `provider=openai-compatible`, `base_url=<서비스의 /v1 주소>`, `model=<모델 ID>`,
`api_key_env=<키를 보관한 환경변수 이름>`으로 설정한다. 키 값은 설정 파일에 넣지 않는다.
Groq 등 무료 API도 같은 계약으로 연결할 수 있다. 계정별 무료 한도·모델 지원 여부는 사용 시 확인한다.
JSON Schema를 지원하지 않는 서비스는 `response_format=json_object`를 명시할 수 있다. 출력은 어느
방식이든 같은 로컬 Pydantic 계약으로 검사하며 실패 시 형식을 바꿔 재호출하지 않는다.

생성 모델에는 업무·정책·변경 값, A 경로의 별칭과 지원 한계, 검색된 카드 제목·요약·근거 종류,
허용 추가 시험 목록을 보낸다. 생성 모델에는 도구를 제공하지 않는다. 모델이 반환할 수 있는 것은
등록 candidate ID, 카드 키, A 경로 별칭과 선택 이유뿐이다. 원문 파일 전체, 자격, 평가 라벨,
case 이름과 기대 판정은 전달하지 않는다. 필드명 기반 redaction의 한계는 임베딩과 같다.

필수 세 시험(normal, other-order, claimed-owner)을 먼저 확보하고 남은 자리에 고객 ID만 사칭하는
claimed-customer와 tenant만 사칭하는 claimed-tenant를 모델 순위로 선택한다. 요청은 사전 등록된
bytes만 사용한다. 정상 입력은 모든 계획에서 유지한다. 후보 매핑은 버전·해시로 등록되어 있다.

검색 성공이지만 적격 카드가 없으면 EMPTY_RETRIEVAL과 필수 시험만 기록한다. 검색·모델·DB 오류,
잘못된 제안, 필수 예산 부족은 C 실행 전에 실패한다. 모델 호출은 사례당 최대 한 번이다.
추가 시험이 부적격이거나 예산이 없으면 모델을 호출하지 않고 그 이유를 기록한다.

`selection.json`에 전체 후보 결정과 제안·근거·예산·모델 metadata를 남긴다. 각 TestPlan에도 같은
selection 기록을 넣고 계획 전체 내용으로 content-v1 ID를 만든다. 기존 plan ID도 읽을 수 있다.
생성 호출 실패·중단은 generation-call.json에 남긴다. 생성 모델은 최종 보안 판정을 제공하지 않는다.
호출 영수증의 called는 provider 함수 호출 시도를 뜻한다. 실패 영수증만으로 모델 서버가 요청을
받거나 완료했다고 판단하지 않는다. 완료 호출은 SUCCEEDED와 응답 metadata·해시로 구별한다.

로컬 Ollama는 실제 모델 digest를 확인한다. 호환 API의 model_revision은 운영자가 고정한 식별자이며,
서비스가 제공한 reported_model/system_fingerprint도 기록한다. 불변 버전을 확인할 수 없으면 그 한계를
명시한다. seed와 temperature가 같아도 재호출의 동일성을 보장하지 않는다. 저장된 KB·retrieval과
제안·metadata는 select_trials(replay=GenerationResult(...))로 모델 없이 재검증할 수 있다.

## 호환 임베딩 endpoint

기본은 기존 Ollama Qwen3 embedding이다. 별도 1024차원 호환 endpoint를 쓰려면
`FLOWDELTA_EMBEDDING_PROVIDER=openai-compatible`, BASE_URL, MODEL, MODEL_DIGEST를 설정한다.
API 키가 필요하면 `FLOWDELTA_EMBEDDING_API_KEY_ENV`에 환경변수 이름을 지정한다. MODEL_DIGEST는
운영자가 확인한 모델 revision이다. 서비스가 실제 같은 가중치를 제공함을 API만으로 증명하지 않는다.
각 vector는 L2 정규화하고 plain-query-v1 profile로 분리한다. query/document 모두 같은 공간을 쓰며,
기존 Qwen instruction profile과 섞지 않는다.

## 평가 실행기 입출력

`flowdelta run-evaluation-attempt request.json --out var/attempt-001 [--execute]`가 외부 실행기용
한 번의 호출이다. 요청에는 `schema_version=0.4.0`, `request_format=flowdelta-evaluation-request-v1`,
`arm`, `attempt`, `seed`, 선택할 `pair_ids`, 선택 방식의 `planning` 설정을 넣는다.
전체 반복·재시도·자료 배정은 외부 실행기가 관리한다. truth·기대 판정 필드는 받지 않는다.

- `fixed`: 기존 세 필수 시험. planning은 생략한다.
- `graph+retrieval+llm`: VECTOR와 generation이 있는 planning 설정. seed를 생성 호출에 전달한다.
- 나머지 arm: 실행·모델 호출 없이 UNSUPPORTED 기록. 기존 사전 설정의 graph+retrieval과 이번
  LLM 결합 구현을 같은 실험 방식으로 표시하지 않는다. 전체 비교 방법 구현과 통계 분석은 별도다.

`attempts.json`은 #107 집계기가 읽는 목록이다. `(등록 case ID)/(scenario ID)`를 case_id로 써서
서로 다른 시험의 before/after를 합치지 않는다. arm과 외부 attempt를 유지하며 C 내부 재시도 번호는
runs[].c_attempt_number로 별도 기록한다. 실행하지 않은 후보도 before/after=null로 남긴다.
NOT_SELECTED, NOT_RUN, FAILED, UNSUPPORTED, INCOMPLETE와 정상 업무·수집·정리 상태를 보존한다.
판정기 INCONCLUSIVE/UNSUPPORTED도 원래 값으로 내보낸다. 누락을 안전으로 바꾸지 않는다.
수집 상태는 C의 ExecutionRun에서 trial.run_records와 export runs[]로 전달한다. 기존 v1 판정
records에 필드를 추가하지 않으므로 과거 저장 증거를 같은 계약으로 계속 재검증할 수 있다.

`result.json`에서 request·attempts 해시와 엔진 요약을, 각 행에서 선택·plan·bundle·judgment 참조와
해시·run ID를 읽는다. DB에서 B 결과를 조회·내보내면 작은 등록 scenario.json도 plan에 고정된
bytes로 복원된다. 임베딩 DB나 생성 서버가 없어도 저장된 계획과 요청 파일을 C에 전달할 수 있다.

스키마: schemas/planning-config-0.4.0.schema.json, schemas/plan-selection-0.4.0.schema.json,
schemas/evaluation-request-0.4.0.schema.json. 작은 고정 방식 요청은 examples/planning/fixed-attempt.json에 있다.

시간 예산은 사례별 계획 시작부터 C 명령까지 적용한다. 남은 시간이 없으면 새 시험을 시작하지 않고,
진행 중 명령의 timeout도 남은 시간으로 제한한다. 컨테이너·네트워크·볼륨 정리와 잔여 확인은 예산
만료 뒤에도 수행하며 해당 상태를 기록한다. model 호출은 자체 timeout과 토큰 상한도 함께 적용한다.
