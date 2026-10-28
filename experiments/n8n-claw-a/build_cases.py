"""Build A's case bundles for the two n8n-claw comparison pairs from hash-pinned public sources.

The workflow files are the exact public bytes. Task, policy, environment, initial state and
provenance are A's stand-ins for static analysis until the input owner's fixed profile replaces
them. Nothing is executed; sources are fetched by commit and checked by SHA-256 and size.
"""

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

REPO = "freddy-schuetz/n8n-claw"
SOURCE_PATH = "workflows/n8n-claw-agent.json"
# Same commits, hashes and sizes as experiments/n8n-claw-memory/sources.json (PR #57); the
# folder names are that collector's layout, so its output can be reused with --sources.
SOURCES = {
    "9ebf36b052b73457acab0fcead254e48986cad01": (
        "before-feature",
        "a961469f64375c6867405db6f048a901387d8c370ce4200f01f262bd2412f99e",
        75657,
    ),
    "63a7f2ee9c9e42fc6a3c4527fa2a7b7f12859377": (
        "feature",
        "7d1e40c8fb3a6b73538718fd2109cd7428347cd0e319ccee2a5976392b3c47ae",
        81027,
    ),
    "4d1b1b748cdc76f6c61abb7c40ee8b01b560ef44": (
        "before-fix",
        "5f297ee69e45fba5b810ce41899cb4b581d74675dbba004d78a979538feb8fc2",
        107030,
    ),
    "eb6d7bc185eaecdf96885b3e17ef1f8dac93a852": (
        "first-fix",
        "c3abd08547a9db5503310a7a3056b0fe49ab700d96aa1b6d78801d260cd885aa",
        110981,
    ),
}
# The two pairs are different comparisons and are never merged into one.
PAIRS = {
    "feature-add": (
        "9ebf36b052b73457acab0fcead254e48986cad01",
        "63a7f2ee9c9e42fc6a3c4527fa2a7b7f12859377",
        "전용 기억 수정·삭제 도구와 AI 연결 추가",
    ),
    "first-fix": (
        "4d1b1b748cdc76f6c61abb7c40ee8b01b560ef44",
        "eb6d7bc185eaecdf96885b3e17ef1f8dac93a852",
        "기억 도구에 소유자·관리자 검사 추가",
    ),
}
STAND_IN = "A 분석 재현용 임시 값이며 입력 담당의 고정 profile(#48)이 정해지면 그것으로 바꾼다."


def verified(data: bytes, commit: str) -> bytes:
    _, sha256, size = SOURCES[commit]
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"SOURCE_INTEGRITY_MISMATCH {commit}")
    return data


def source(commit: str, sources: Path | None) -> bytes:
    folder, _, size = SOURCES[commit]
    if sources is not None:
        path = sources / folder / SOURCE_PATH
        if path.is_symlink() or path.is_junction():
            raise ValueError("LINK_SOURCE_REJECTED")
        with path.open("rb") as stream:
            return verified(stream.read(size + 1), commit)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("UNPINNED_SOURCE")
    url = f"https://raw.githubusercontent.com/{REPO}/{commit}/{SOURCE_PATH}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return verified(response.read(size + 1), commit)


def document(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def case_files(pair: str, before: str, after: str, change: str) -> dict[str, bytes]:
    urls = [f"https://github.com/{REPO}/blob/{commit}/{SOURCE_PATH}" for commit in (before, after)]
    return {
        "case.json": document(
            {
                "schema_version": "0.4.0",
                "case_id": f"n8n-claw-memory-{pair}",
                "family_id": "n8n-claw-memory",
                "before_ref": "workflow/before.json",
                "after_ref": "workflow/after.json",
                "task_ref": "task.json",
                "evaluation_policy_ref": "policy.json",
                "environment_ref": "environment.json",
                "initial_state_ref": "state/initial.json",
                "provenance_ref": "provenance.json",
                "changed_axes": ["workflow"],
                "baseline_security_status": "UNKNOWN",
                "obligations": [
                    {
                        "schema_version": "0.4.0",
                        "obligation_id": "owner-scoped-memory-change",
                        "required": True,
                        "description": "일반 사용자의 요청으로는 요청자 본인 소유 기억만 "
                        "수정·삭제된다. 관리자는 다른 사용자의 기억을 삭제할 수 있다.",
                        "required_artifacts": ["initial-state", "final-state"],
                    }
                ],
            }
        ),
        "task.json": document(
            {
                "schema_version": "0.4.0",
                "task_id": "n8n-claw-memory",
                "purpose": "사용자가 대화 agent에게 자신의 장기 기억을 "
                "검색·수정·삭제하도록 요청한다.",
                "actor": f"합성 일반 사용자. {STAND_IN}",
                "normal_input": {},
                "success_conditions": ["요청자 본인 소유 기억만 수정·삭제된다."],
                "protected_resources": ["memory_long의 다른 사용자 기억"],
                "allowed_side_effects": ["요청자 본인 기억의 검색·수정·삭제", "대화 응답"],
                "provenance_ref": "provenance.json",
            }
        ),
        "policy.json": document(
            {
                "schema_version": "0.4.0",
                "policy_id": "n8n-claw-memory-a-policy",
                "policy_version": "a-stand-in-1",
                "interpretation": "DECLARED_ONLY",
                "provenance_ref": "provenance.json",
                "rules": [
                    {
                        "schema_version": "0.4.0",
                        "rule_id": "own-memory-only",
                        "subject": "일반 사용자",
                        "resource": "memory_long",
                        "condition": "요청자 본인이 소유한 기억만 수정·삭제한다.",
                        "exceptions": ["관리자는 다른 사용자의 기억을 삭제할 수 있다."],
                        "rationale": f"#57 README의 연구 정책. {STAND_IN}",
                    }
                ],
            }
        ),
        "environment.json": document(
            {
                "schema_version": "0.4.0",
                "environment_id": "n8n-claw-a-static",
                "engine": "n8n",
                # The team's core adapter baseline; not the version n8n-claw runs on.
                "target_version": "2.42.6",
                "runtime_status": "NOT_VALIDATED",
                "service_bindings": {},
                "extensions": {"flowdelta.semantic_profile": "core-local-v1"},
            }
        ),
        "state/initial.json": document(
            {"schema_version": "0.4.0", "state_id": "n8n-claw-memory-a-state", "data": {}}
        ),
        "provenance.json": document(
            {
                "schema_version": "0.4.0",
                "provenance_id": f"n8n-claw-memory-{pair}-a",
                "source_kind": "PUBLIC_ADAPTED",
                "source": f"{REPO} {SOURCE_PATH}, {before[:7]} → {after[:7]} ({change})",
                "reuse_status": "원본 저장소에 별도 LICENSE가 없어(#57 조사) 원문은 저장소에 "
                "커밋하지 않고 고정 commit URL과 SHA-256으로만 참조한다.",
                "transformations": [
                    "workflow는 원본 바이트를 그대로 쓴다.",
                    f"task·policy·environment·초기 상태는 {STAND_IN}",
                ],
                "production_equivalence": "NOT_CLAIMED",
                "extensions": {"flowdelta.source_urls": urls},
            }
        ),
    }


def manifest(pair: str, files: dict[str, bytes]) -> bytes:
    return document(
        {
            "schema_version": "0.4.0",
            "artifacts": [
                {
                    "schema_version": "0.4.0",
                    "artifact_id": f"claw-{pair}-source-{index}",
                    "path": path,
                    "kind": "source",
                    "media_type": "application/json",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                    "extensions": {},
                }
                for index, (path, data) in enumerate(sorted(files.items()))
            ],
        }
    )


def write(root: Path, files: dict[str, bytes]) -> None:
    for path, data in sorted(files.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)


def build(out: Path, sources: Path | None) -> None:
    # A fresh folder only, so an earlier run is never mixed into this one.
    out.mkdir(parents=False, exist_ok=False)
    for pair, (before, after, change) in PAIRS.items():
        files = case_files(pair, before, after, change)
        files["workflow/before.json"] = source(before, sources)
        files["workflow/after.json"] = source(after, sources)
        files["manifest.json"] = manifest(pair, files)
        write(out / pair / "case", files)
    print(
        json.dumps({"status": "CASES_WRITTEN", "pairs": list(PAIRS), "execution_status": "NOT_RUN"})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True, help="New folder; parent must exist.")
    parser.add_argument(
        "--sources", type=Path, help="Folder written by experiments/n8n-claw-memory/collect.py."
    )
    arguments = parser.parse_args()
    build(arguments.out, arguments.sources)
