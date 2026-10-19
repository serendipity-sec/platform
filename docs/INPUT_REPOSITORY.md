# 고정 profile 입력 저장·조회

[#48](https://github.com/serendipity-sec/platform/issues/48)의 입력 저장 경계다.
`MemoryInputRepository`와 `FileInputRepository`는 같은 `InputRepository` protocol을 구현한다.
고정 profile 검사를 통과한 입력의 원문, case, snapshot, obligation, source artifact 참조를
기존 Handoff 형태로 저장하고 정확한 기록으로 읽는다.

```python
from pathlib import Path
from flowdelta.graph import analyze
from flowdelta.ingestion import json_bytes
from flowdelta.input_repository import FileInputRepository

repository = FileInputRepository(Path("var/inputs"))
record = repository.save(
    Path("examples/order-lookup"),
    Path("var/order-profile.json"),
    expected_profile_sha256="검토 후 보관한 profile_sha256",
)
Path("var/input-record.json").write_bytes(json_bytes(record.model_dump(mode="json")))
handoff = repository.get(record)
analysis = analyze(handoff)
source = repository.get_artifact(record, handoff.source_manifest.artifacts[0].artifact_id)
```

profile 생성과 해시는 [FIXED_PROFILE.md](FIXED_PROFILE.md)를 따른다.
저장 요청이 후보의 profile을 자동 재생성하지 않는다. `record`는 저장 시 돌려받은 신뢰하는
영수증이며, 조회 전에 후보가 제공한 값으로 바꾸지 않는다. 재시작 후에는
`InputRecord.model_validate_json(Path(...).read_bytes())`로 보관한 영수증을 읽는다.

## ID와 무결성

- namespace는 **profile 내용 해시 + case ID**다. Windows에서 대소문자가 다른 case ID도
  별개의 기록으로 보존하도록 실제 디렉터리 이름은 두 값의 해시로 만든다.
- 같은 profile/case의 같은 내용을 다시 저장하면 같은 기록을 돌려준다. 다른 후보·출처·내용으로
  바꿔 저장하면 `INPUT_ID_CONFLICT`다. 수정 후보는 새 case ID를 사용한다.
- `InputRecord`는 profile ID/revision/hash, case, 전후 snapshot ID, Handoff 해시와
  원본 manifest 해시를 보존한다. 같은 case의 '최신 결과'를 읽는 API는 없다.
- 파일 adapter는 기존 manifest와 원본 bytes를 보존한다. 조회 시 각 파일 해시와
  호출자가 보관한 기록의 Handoff 해시를 확인한다. record나 manifest를 고쳐도 정상 조회로 바뀌지 않는다.
- 파일 저장 중 실패하면 `record.json` 없는 미완료 폴더가 남을 수 있다. 완료 기록으로 읽거나
  자동 덮어쓰기하지 않는다. 원인을 확인한 뒤 해당 로컬 실패 자료를 별도로 정리해야 한다.
- 메모리 adapter는 원문 bytes를 보존하고 조회 때 새 모델을 반환한다. 소비자의 모델 수정은
  저장된 입력을 바꾸지 않는다. `get_artifact`는 전달된 artifact ID만 읽는다.

## 공용 DB로 이어갈 범위

현재 adapter는 로컬 파일/메모리다. 개인 PC 경로를 팀 공용 artifact URL로 등록하지 않는다.
B #50의 공용 연결·migration 순서와 artifact 접근 경로가 정해지면 입력 영역의 DB adapter가
같은 protocol과 충돌/해시 검사를 구현해야 한다. 이 PR은 DB 접속·migration 적용·다른 팀원의
공용 저장소 조회 완료를 주장하지 않는다.

실제 A 함수가 저장소에서 읽은 Handoff를 소비하는 테스트를 포함한다. 선정 n8n-claw의
정책 binding, AI 도구 의미, B/C 검사 계약과 실행 지원은 별도 조건이다.
