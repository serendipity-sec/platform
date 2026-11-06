# 로컬 임베딩

현재 provider 계약은 `flowdelta.knowledge.embedding.EmbeddingProvider`다. 검색·색인 호출은
일반 텍스트와 `DOCUMENT` 또는 `QUERY` 역할을 넘긴다. 구현은 벡터와 함께 provider, 모델,
실제 Ollama 모델 digest, 차원, query instruction 지문을 돌려준다. 같은 provider·모델 digest·차원·
정규화·지시문을 쓰는 문서와 질의 프로필은 같은 `profile_id`를 가진다.
`embedding_profiles_compatible`로 색인과 검색 프로필의 호환성을 확인할 수 있다. 질의 벡터는 색인에
저장하지 않는다.

기본 profile은 Ollama `qwen3-embedding:0.6b`, 1024차원이다. Qwen의 공식 사용 가이드에 맞춰
document에는 지시문을 붙이지 않고 query에만 검색 의도를 영어로 붙인다. 이 규칙을 바꾸면
지시문 지문이 달라져 새 profile/색인으로 취급한다. Ollama에는 `truncate=false`를
보내고 한 입력은 UTF-8 128 KiB 이내로 제한한다. 응답 벡터는 provider 경계에서 L2 단위 길이로
정규화한다. 이 입력 byte 제한은 토큰 수를 대신하지 않는다. 모델
컨텍스트 제한을 넘은 요청은 Ollama 오류로 처리하며 내용을 자르거나 다른 결과로 위장하지 않는다.

## 로컬 Docker 시작 (PowerShell)

```powershell
docker compose -f runtime/embedding/compose.yaml up -d --wait
docker compose -f runtime/embedding/compose.yaml exec ollama ollama pull qwen3-embedding:0.6b
uv run --frozen --extra baseline flowdelta embedding-smoke
```

Ollama는 localhost:11434로만 열리고 모델 데이터는 named volume에 보관된다. 최초 pull에는
모델 파일 다운로드가 필요하다. 실제 모델 digest는 서버의 model tag 목록에서 읽어 실행 결과에
포함한다. 모델 파일과 vector 내용을 로그·repo·smoke 출력에 포함하지 않는다.

설정은 `FLOWDELTA_EMBEDDING_BASE_URL`, `FLOWDELTA_EMBEDDING_MODEL`,
`FLOWDELTA_EMBEDDING_DIMENSIONS`, `FLOWDELTA_EMBEDDING_TIMEOUT`,
`FLOWDELTA_EMBEDDING_BATCH_SIZE`로 조절한다. token/API key 설정은 없다.

Smoke는 안전한 합성 입력 1개를 문서 역할로, 1개를 질의 역할로 임베딩한다. 성공 시 provider,
모델 digest, 차원, profile ID와 vector 개수만 표시하며 실제 텍스트나 vector는 출력하지 않는다.
생성형 LLM·카드 작성 기능은 수행하지 않는다.

`OllamaEmbeddingProvider.profile()`은 `/api/ps`의 현재 실행 model digest를 읽어 설정된 provider,
model, dimensions, query instruction 지문, normalization을 포함한 `EmbeddingProfile`을 만든다.
이 메서드는 텍스트를 임베딩하지 않으며, 색인 명령이 기존 카드 DOCUMENT를 profile 확인만을 위해
다시 보내지 않도록 한다. 모델이 실행 중이지 않으면 `EMBEDDING_MODEL_NOT_FOUND`로 실패한다.
따라서 cold start에서는 위 `embedding-smoke`를 먼저 실행한다.

카드 색인은 bundle root 기준 source provenance를 검증한다.

```sh
uv run --frozen --extra knowledge flowdelta knowledge index-cards <analysis-bundle> \
  --cards <cards-dir>
```

색인은 동일한 `(card_id, version)`과 canonical content hash를 재사용한다. 동일 ID/version의
내용이 바뀌면 거부하므로 내용을 수정할 때 version을 올린다. model digest나 query instruction이
바뀌면 새로운 1024차원 embedding profile이다. `index-cards`를 다시 실행해 새 profile 벡터를
만든다. 이전 profile 벡터는 명시적인 과거 profile 검색을 위해 별도 key 아래 남는다. 과거 plan
replay는 저장된 `kb-snapshot.json`과 B output bytes를 쓰므로 vector search를 반복하지 않는다.
모델이 실행 중이지만 같은 profile이 아직 색인되지 않았다면 `plan --shared-cards`는 명시적 profile
미색인 오류를 내고 keyword 검색으로 대체하지 않는다.
같은 profile을 동시에 색인하는 요청은 `CARD_CATALOG_PROFILE_ALREADY_INDEXING`으로 빠르게 실패할 수
있다. 기존 작업이 끝나면 동일 명령을 재시도한다.
현재 카드 파일은 각각 8 MiB, 전체 디렉터리는 32 MiB, embedding 입력은 128 KiB로 제한한다.
카드 DOCUMENT batch는 `FLOWDELTA_EMBEDDING_BATCH_SIZE` 설정을 따르되 최대 16개로 제한한다.
공유 검색은 최대 100개 후보이며 반환 카드 bytes 합계가 8 MiB를 넘으면 실패한다.

## 검증

`pytest tests/knowledge/test_embedding.py`는 로컬 mock HTTP server와 mock provider로 query와
document 처리 차이, profile 변경, digest/차원/vector 검증, batch 제한 및 provider 오류의 비노출을
검사한다. 실 모델 검증은 Docker Ollama의 smoke 명령으로 별도로 실행한다.

## 데이터 전송과 이용 조건

기본 Ollama 주소는 `127.0.0.1`이다. 합성 fixture를 사용하면 네트워크 밖으로 원문을 보내지 않는다.
실제 `plan --shared-cards`는 구조화된 분석 before/after change 값을 설정한 endpoint에 QUERY로
전달한다. credential 종류와 민감해 보이는 필드명은 redaction marker로 바뀌지만 heuristic이므로
비밀을 완전히 찾는다고 보장할 수 없다. 신뢰된 endpoint와 검토한 분석 내용만 사용한다. request
body와 vector는 CLI 로그에 출력하지 않는다. endpoint를 바꾸면 해당 서버로 입력 텍스트가 전달될
수 있으므로 사전에 전송 필드와 허가 범위를 정한다. 모델 metadata는 Apache-2.0이며 Ollama 서버는
MIT다. 각 원문·고지는 `THIRD_PARTY.md`에 기록한다.
