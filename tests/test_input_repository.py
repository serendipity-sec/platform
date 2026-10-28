import json
from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.fixed_profile import freeze_profile, profile_digest, write_profile
from flowdelta.graph import analyze
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.input_repository import FileInputRepository, InputRecord, MemoryInputRepository


@pytest.fixture(params=["memory", "file"])
def repository(request, tmp_path):
    return (
        MemoryInputRepository()
        if request.param == "memory"
        else FileInputRepository(tmp_path / "repository")
    )


def setup_profile(bundle: Path, tmp_path: Path):
    profile = freeze_profile(load_case(bundle), profile_id="test", revision="1.0.0")
    path = tmp_path / "profile.json"
    write_profile(profile, path, input_root=bundle)
    return path, profile_digest(profile)


def test_exact_record_artifact_round_trip_and_a_consumption(repository, bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    record = repository.save(bundle, profile, expected_profile_sha256=checksum)
    assert repository.save(bundle, profile, expected_profile_sha256=checksum) == record
    handoff = repository.get(record)
    assert analyze(handoff).case_id == record.case_id
    for ref in handoff.source_manifest.artifacts:
        assert repository.get_artifact(record, ref.artifact_id) == (bundle / ref.path).read_bytes()
    handoff.case.case_id = "consumer-mutation"
    assert repository.get(record).case.case_id == record.case_id


def test_same_profile_case_with_different_candidate_is_conflict(repository, bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    record = repository.save(bundle, profile, expected_profile_sha256=checksum)
    edit_document(bundle, "workflow/after.json", lambda v: v.update(name="new-candidate"))
    with pytest.raises(InputError, match="INPUT_ID_CONFLICT"):
        repository.save(bundle, profile, expected_profile_sha256=checksum)
    assert repository.get(record).case.case_id == record.case_id


def test_case_ids_do_not_alias_on_case_insensitive_filesystems(repository, bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    edit_document(bundle, "case.json", lambda v: v.update(case_id="CASE-1"))
    first = repository.save(bundle, profile, expected_profile_sha256=checksum)
    edit_document(bundle, "case.json", lambda v: v.update(case_id="case-1"))
    second = repository.save(bundle, profile, expected_profile_sha256=checksum)
    assert first.record_id != second.record_id
    assert repository.get(first).case.case_id == "CASE-1"
    assert repository.get(second).case.case_id == "case-1"


def test_drift_fails_before_any_record_is_saved(repository, bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    edit_document(bundle, "environment.json", lambda v: v.update(target_version="changed"))
    with pytest.raises(InputError, match="PROFILE_FIXED_INPUT_CHANGED"):
        repository.save(bundle, profile, expected_profile_sha256=checksum)
    if isinstance(repository, FileInputRepository):
        assert not repository.root.exists()


def test_forged_record_cannot_select_latest_or_another_input(repository, bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    record = repository.save(bundle, profile, expected_profile_sha256=checksum)
    with pytest.raises(InputError, match="INPUT_RECORD_ID_MISMATCH"):
        repository.get(record.model_copy(update={"case_id": "other"}))
    with pytest.raises(InputError, match="INPUT_RECORD_INTEGRITY_MISMATCH"):
        repository.get(record.model_copy(update={"handoff_sha256": "0" * 64}))
    with pytest.raises(InputError, match="INPUT_ARTIFACT_NOT_FOUND"):
        repository.get_artifact(record, "not-supplied")


def test_file_corruption_and_incomplete_save_fail_explicitly(bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    record = repo.save(bundle, profile, expected_profile_sha256=checksum)
    source = repo.root / record.record_id / "handoff" / "task.json"
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        repo.get(record)
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        repo.save(bundle, profile, expected_profile_sha256=checksum)
    (repo.root / record.record_id / "record.json").unlink()
    with pytest.raises(InputError, match="INPUT_NOT_FOUND_OR_INCOMPLETE"):
        repo.get(record)


def test_record_file_tampering_cannot_override_callers_receipt(bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    record = repo.save(bundle, profile, expected_profile_sha256=checksum)
    changed = record.model_copy(update={"handoff_sha256": "0" * 64})
    path = repo.root / record.record_id / "record.json"
    path.write_bytes(json_bytes(changed.model_dump(mode="json")))
    with pytest.raises(InputError, match="INPUT_RECORD_INTEGRITY_MISMATCH"):
        repo.get(record)


def test_repository_cannot_overlap_source(bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    with pytest.raises(InputError, match="OUTPUT_OVERLAPS_INPUT"):
        FileInputRepository(bundle / "storage").save(
            bundle, profile, expected_profile_sha256=checksum
        )


def test_two_profile_revisions_do_not_share_namespace(bundle, tmp_path):
    profile_path, checksum = setup_profile(bundle, tmp_path)
    repo = MemoryInputRepository()
    first = repo.save(bundle, profile_path, expected_profile_sha256=checksum)
    second_profile = freeze_profile(load_case(bundle), profile_id="test", revision="2.0.0")
    path = tmp_path / "profile2.json"
    write_profile(second_profile, path, input_root=bundle)
    second = repo.save(bundle, path, expected_profile_sha256=profile_digest(second_profile))
    assert first.record_id != second.record_id
    assert repo.get(first).case.case_id == repo.get(second).case.case_id


def test_invalid_profile_pin_is_rejected_before_storage(bundle, tmp_path):
    profile, _ = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    with pytest.raises(InputError, match="PROFILE_IDENTITY_MISMATCH"):
        repo.save(bundle, profile, expected_profile_sha256="0" * 64)
    assert not repo.root.exists()


def test_interrupted_write_has_no_complete_record(bundle, tmp_path, monkeypatch):
    from flowdelta import input_repository

    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")

    original = input_repository.write_handoff

    def fail(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(input_repository, "write_handoff", fail)
    with pytest.raises(InputError, match="INPUT_REPOSITORY_WRITE_ERROR"):
        repo.save(bundle, profile, expected_profile_sha256=checksum)
    assert not list(repo.root.glob("*/record.json"))
    monkeypatch.setattr(input_repository, "write_handoff", original)
    record = repo.save(bundle, profile, expected_profile_sha256=checksum)
    assert repo.get(record).case.case_id == record.case_id


def test_record_schema_matches_export():
    path = Path(__file__).parents[1] / "schemas/input-record-v1.schema.json"
    assert path.read_bytes() == json_bytes(InputRecord.model_json_schema())


def test_adapters_accept_valid_per_document_depth(repository, bundle, tmp_path):
    nested = "leaf"
    for _ in range(62):
        nested = {"n": nested}
    edit_document(bundle, "task.json", lambda v: v.update(extensions={"test.deep": nested}))
    profile, checksum = setup_profile(bundle, tmp_path)
    record = repository.save(bundle, profile, expected_profile_sha256=checksum)
    assert repository.get(record).task.extensions["test.deep"] == nested


@pytest.mark.parametrize("invalid", ["generated_path", "normalized_depth", "bundle_size"])
def test_adapters_reject_unstorable_handoffs_without_reserving_id(
    repository, bundle, tmp_path, invalid
):
    if invalid == "generated_path":
        (bundle / "environment.json").rename(bundle / "semantics.json")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        for ref in manifest["artifacts"]:
            if ref["path"] == "environment.json":
                ref["path"] = "semantics.json"
        manifest_path.write_bytes(json_bytes(manifest))
        edit_document(bundle, "case.json", lambda v: v.update(environment_ref="semantics.json"))
        error = "GENERATED_PATH_COLLISION"
    elif invalid == "normalized_depth":
        nested = "leaf"
        for _ in range(63):
            nested = {"n": nested}
        # Normalization wraps unknown top-level fields in opaque_fields, adding a level.
        edit_document(bundle, "workflow/after.json", lambda v: v.update(deep=nested))
        error = "JSON_DEPTH_LIMIT"
    else:
        for side in ("before", "after"):
            edit_document(
                bundle, f"workflow/{side}.json", lambda v: v.update(padding="x" * 7_900_000)
            )
        edit_document(
            bundle,
            "state/initial.json",
            lambda v: v.update(extensions={"test.padding": "x" * 2_000_000}),
        )
        error = "BUNDLE_SIZE_LIMIT"
    profile, checksum = setup_profile(bundle, tmp_path)
    original_bytes = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*.json")}
    # Freeze/load accepts the source; serialization/read-back must reject it on both adapters.
    for _ in range(2):
        with pytest.raises(InputError, match=f"^{error}$"):
            repository.save(bundle, profile, expected_profile_sha256=checksum)
    assert original_bytes == {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*.json")}
    if isinstance(repository, FileInputRepository):
        assert not repository.root.exists() or not list(repository.root.iterdir())
    else:
        assert not repository._entries
    if invalid == "normalized_depth":
        # Candidate-only repair retains the same profile and case namespace.
        edit_document(bundle, "workflow/after.json", lambda v: v.pop("deep"))
        record = repository.save(bundle, profile, expected_profile_sha256=checksum)
        assert repository.get(record).case.case_id == record.case_id


def test_file_checks_readability_before_publication(bundle, tmp_path, monkeypatch):
    from flowdelta import pipeline_io

    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    monkeypatch.setattr(pipeline_io, "MAX_TOTAL_BYTES", 100)
    with pytest.raises(InputError, match="BUNDLE_SIZE_LIMIT"):
        repo.save(bundle, profile, expected_profile_sha256=checksum)
    assert not list(repo.root.glob("input-*"))


def test_relative_repository_root_survives_cwd_change(bundle, tmp_path, monkeypatch):
    profile, checksum = setup_profile(bundle, tmp_path)
    monkeypatch.chdir(tmp_path)
    repo = FileInputRepository(Path("repository"))
    record = repo.save(bundle, profile, expected_profile_sha256=checksum)
    monkeypatch.chdir(bundle)
    assert repo.get(record).case.case_id == record.case_id


def test_concurrent_identical_saves_publish_one_complete_record(bundle, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from flowdelta import input_repository

    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    barrier = Barrier(2)
    original = input_repository.write_handoff

    def delayed(*args):
        barrier.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(input_repository, "write_handoff", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(repo.save, bundle, profile, expected_profile_sha256=checksum)
            for _ in range(2)
        ]
        records = [future.result() for future in futures]
    assert records[0] == records[1]
    assert repo.get(records[0]).case.case_id == records[0].case_id
    assert len(list(repo.root.iterdir())) == 1


def test_missing_record_code_is_identical_between_adapters(bundle, tmp_path):
    profile, checksum = setup_profile(bundle, tmp_path)
    record = MemoryInputRepository().save(bundle, profile, expected_profile_sha256=checksum)
    root = tmp_path / "empty"
    root.mkdir()
    for repo in (MemoryInputRepository(), FileInputRepository(root)):
        with pytest.raises(InputError, match="^INPUT_NOT_FOUND$"):
            repo.get(record)


def test_receipt_write_failure_removes_only_own_stage_and_is_retryable(
    bundle, tmp_path, monkeypatch
):
    from flowdelta import input_repository

    profile, checksum = setup_profile(bundle, tmp_path)
    repo = FileInputRepository(tmp_path / "repository")
    repo.root.mkdir()
    unrelated = repo.root / "unrelated"
    unrelated.write_text("preserve")
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("injected receipt failure")

        patch.setattr(input_repository, "write_new_bytes", fail)
        with pytest.raises(InputError, match="INPUT_REPOSITORY_WRITE_ERROR"):
            repo.save(bundle, profile, expected_profile_sha256=checksum)
    assert list(repo.root.iterdir()) == [unrelated]
    record = repo.save(bundle, profile, expected_profile_sha256=checksum)
    assert repo.get(record).case.case_id == record.case_id
    assert unrelated.read_text() == "preserve"
