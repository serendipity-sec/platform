from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import ExitStack
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier

import pytest

from flowdelta.graph import analyze
from flowdelta.graph import repository as storage
from flowdelta.graph.repository import (
    AnalysisRecord,
    AnalysisRepository,
    FileAnalysisRepository,
    MemoryAnalysisRepository,
)
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.pipeline_contracts import AnalysisBundle
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2]
Factory = Callable[[Path], AnalysisRepository]
KINDS: dict[str, Factory] = {
    "file": FileAnalysisRepository,
    "memory": lambda _: MemoryAnalysisRepository(),
}


def analysis(case: str) -> AnalysisBundle:
    return analyze(read_handoff(ROOT / "examples/contracts" / case / "input"))


@pytest.fixture(params=sorted(KINDS))
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> AnalysisRepository:
    return KINDS[request.param](tmp_path / "analyses")


def test_round_trip_keeps_the_exact_analysis(repository: AnalysisRepository) -> None:
    bundle = analysis("support-triage")
    record = repository.save(bundle)
    loaded = repository.get(bundle.analysis_id)
    # Limitations, impact paths and extensions survive storage byte for byte.
    assert json_bytes(loaded.model_dump(mode="json")) == json_bytes(bundle.model_dump(mode="json"))
    assert (record.analysis_id, record.case_id) == (bundle.analysis_id, bundle.case_id)
    assert (record.before_snapshot_id, record.after_snapshot_id) == (
        bundle.before_snapshot_id,
        bundle.after_snapshot_id,
    )
    assert record.producer == bundle.authorship.producer
    assert record.sha256 == digest(json_bytes(bundle.model_dump(mode="json")))
    assert record.limitations == bundle.limitations
    assert record.extensions == bundle.extensions


def test_saving_the_same_analysis_again_is_a_no_op(repository: AnalysisRepository) -> None:
    bundle = analysis("order-lookup")
    assert repository.save(bundle) == repository.save(bundle)


def test_same_id_with_other_content_is_refused(repository: AnalysisRepository) -> None:
    bundle = analysis("order-lookup")
    repository.save(bundle)
    changed = bundle.model_copy(update={"limitations": [*bundle.limitations, "다른 내용"]})
    with pytest.raises(InputError) as error:
        repository.save(changed)
    assert error.value.code == "ANALYSIS_ID_CONFLICT"
    assert repository.get(bundle.analysis_id) == bundle


def test_unknown_id_is_not_found(repository: AnalysisRepository) -> None:
    with pytest.raises(InputError) as error:
        repository.get("analysis-0000000000000000")
    assert error.value.code == "ANALYSIS_NOT_FOUND"


def test_find_matches_the_exact_case_and_snapshots(repository: AnalysisRepository) -> None:
    order, triage = analysis("order-lookup"), analysis("support-triage")
    repository.save(order)
    repository.save(triage)
    found = repository.find(order.case_id, order.before_snapshot_id, order.after_snapshot_id)
    assert [record.analysis_id for record in found] == [order.analysis_id]
    # Never falls back to "the latest analysis of the case".
    assert repository.find(order.case_id, order.before_snapshot_id, "other-snapshot") == []


def test_file_repository_survives_a_new_instance(tmp_path: Path) -> None:
    bundle = analysis("order-lookup")
    FileAnalysisRepository(tmp_path).save(bundle)
    assert FileAnalysisRepository(tmp_path).get(bundle.analysis_id) == bundle


def test_file_repository_detects_a_changed_file(tmp_path: Path) -> None:
    bundle = analysis("order-lookup")
    repository = FileAnalysisRepository(tmp_path)
    record = repository.save(bundle)
    stored = tmp_path / record.location
    stored.write_bytes(stored.read_bytes().replace(b'"MODULE_OUTPUT"', b'"CONTRACT_EXAMPLE"'))
    with pytest.raises(InputError) as error:
        repository.get(bundle.analysis_id)
    assert error.value.code == "ANALYSIS_INTEGRITY_MISMATCH"


@pytest.mark.parametrize("corrupt", [b"", b'{"corrupt": true}'])
def test_resaving_does_not_report_success_for_a_corrupt_file(
    tmp_path: Path, corrupt: bytes
) -> None:
    bundle = analysis("order-lookup")
    repository = FileAnalysisRepository(tmp_path)
    record = repository.save(bundle)
    stored = tmp_path / record.location
    stored.write_bytes(corrupt)

    with pytest.raises(InputError) as error:
        FileAnalysisRepository(tmp_path).save(bundle)

    assert error.value.code == "ANALYSIS_INTEGRITY_MISMATCH"
    assert stored.read_bytes() == corrupt


def save_after_empty_scan(
    root: Path, bundle: AnalysisBundle, synchronize: Callable[[], object]
) -> AnalysisRecord | str:
    class RacingRepository(FileAnalysisRepository):
        scanned = False

        def _files(self, analysis_id: str) -> list[Path]:
            files = super()._files(analysis_id)
            if not self.scanned:
                self.scanned = True
                assert files == []
                # Both writers see an unused ID before either publishes its result.
                synchronize()
            return files

    try:
        return RacingRepository(root).save(bundle)
    except InputError as error:
        return error.code


@pytest.mark.parametrize("different_content", [False, True])
@pytest.mark.parametrize("workers", ["threads", "processes"])
def test_concurrent_saves_keep_one_readable_analysis(
    tmp_path: Path, different_content: bool, workers: str
) -> None:
    bundle = analysis("order-lookup")
    other = (
        bundle.model_copy(update={"limitations": [*bundle.limitations, "different content"]})
        if different_content
        else bundle
    )
    with ExitStack() as stack:
        if workers == "processes":
            context = get_context("spawn")
            manager = stack.enter_context(context.Manager())
            synchronize = manager.Barrier(2, timeout=15).wait
            pool = stack.enter_context(ProcessPoolExecutor(max_workers=2, mp_context=context))
        else:
            synchronize = Barrier(2, timeout=15).wait
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=2))
        futures = [
            pool.submit(save_after_empty_scan, tmp_path, item, synchronize)
            for item in (bundle, other)
        ]
        results = [future.result(timeout=30) for future in futures]

    records = [result for result in results if isinstance(result, AnalysisRecord)]
    errors = [result for result in results if isinstance(result, str)]
    assert len(records) == (1 if different_content else 2)
    assert errors == (["ANALYSIS_ID_CONFLICT"] if different_content else [])
    assert all(record == records[0] for record in records)
    repository = FileAnalysisRepository(tmp_path)
    stored = repository.get(bundle.analysis_id)
    assert stored in (bundle, other)
    assert repository.find(bundle.case_id, bundle.before_snapshot_id, bundle.after_snapshot_id) == [
        records[0]
    ]
    assert len(list((tmp_path / bundle.analysis_id).glob("*.json"))) == 1
    assert [path.name for path in tmp_path.iterdir()] == [bundle.analysis_id]


@pytest.mark.parametrize("failure", ["fsync", "rename"])
def test_failed_write_leaves_no_partial_analysis_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repository = FileAnalysisRepository(tmp_path)
    previous = analysis("support-triage")
    repository.save(previous)
    bundle = analysis("order-lookup")

    def fail(*_: object) -> None:
        raise OSError("injected storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, failure, fail)
        with pytest.raises(OSError, match="injected storage failure"):
            repository.save(bundle)

    assert [path.name for path in tmp_path.iterdir()] == [previous.analysis_id]
    assert repository.get(previous.analysis_id) == previous
    repository.save(bundle)
    assert repository.get(bundle.analysis_id) == bundle


def test_readers_see_only_complete_analyses_during_a_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = analysis("order-lookup")
    previous = bundle.model_copy(update={"analysis_id": "analysis-previous"})
    repository = FileAnalysisRepository(tmp_path)
    record = repository.save(previous)
    fsync = storage.os.fsync
    checked = False

    def check_before_publication(descriptor: int) -> None:
        nonlocal checked
        fsync(descriptor)
        reader = FileAnalysisRepository(tmp_path)
        with pytest.raises(InputError) as missing:
            reader.get(bundle.analysis_id)
        assert missing.value.code == "ANALYSIS_NOT_FOUND"
        assert reader.find(bundle.case_id, bundle.before_snapshot_id, bundle.after_snapshot_id) == [
            record
        ]
        checked = True

    monkeypatch.setattr(storage.os, "fsync", check_before_publication)
    repository.save(bundle)
    assert checked
    assert repository.get(bundle.analysis_id) == bundle


@pytest.mark.parametrize("analysis_id", ["..", ".", "a/b", ""])
def test_ids_that_could_leave_the_folder_are_refused(tmp_path: Path, analysis_id: str) -> None:
    with pytest.raises(InputError) as error:
        FileAnalysisRepository(tmp_path).get(analysis_id)
    assert error.value.code == "ANALYSIS_ID_INVALID"


def link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("OS does not grant symlink creation; CI Linux covers this case")


def refused_everywhere(repository: FileAnalysisRepository, bundle: AnalysisBundle) -> None:
    calls: list[Callable[[], object]] = [
        lambda: repository.save(bundle),
        lambda: repository.get(bundle.analysis_id),
        lambda: repository.find(
            bundle.case_id, bundle.before_snapshot_id, bundle.after_snapshot_id
        ),
    ]
    for call in calls:
        with pytest.raises(InputError) as error:
            call()
        assert error.value.code == "ANALYSIS_LINK_REJECTED"


def test_linked_analysis_folder_outside_the_root_is_refused(tmp_path: Path) -> None:
    bundle = analysis("order-lookup")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "analyses"
    root.mkdir()
    link(root / bundle.analysis_id, outside)
    refused_everywhere(FileAnalysisRepository(root), bundle)
    # Nothing was written through the link.
    assert list(outside.iterdir()) == []


def test_linked_analysis_file_outside_the_root_is_refused(tmp_path: Path) -> None:
    bundle = analysis("order-lookup")
    # A valid stored analysis outside the root, so only the link itself is wrong.
    outside = tmp_path / "outside"
    record = FileAnalysisRepository(outside).save(bundle)
    root = tmp_path / "analyses"
    stored = root / record.location
    stored.parent.mkdir(parents=True)
    link(stored, outside / record.location)
    refused_everywhere(FileAnalysisRepository(root), bundle)


def deeply_nested(levels: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": True}
    for _ in range(levels):
        value = {"next": value}
    return value


def test_analysis_that_could_not_be_read_back_is_refused(repository: AnalysisRepository) -> None:
    bundle = analysis("order-lookup")
    deep = bundle.model_copy(update={"extensions": {"test.deep": deeply_nested(70)}})
    with pytest.raises(InputError) as error:
        repository.save(deep)
    assert error.value.code == "JSON_DEPTH_LIMIT"
    # Nothing is stored, so the ID stays free instead of holding an unreadable analysis.
    with pytest.raises(InputError) as missing:
        repository.get(bundle.analysis_id)
    assert missing.value.code == "ANALYSIS_NOT_FOUND"


def test_relative_file_root_is_fixed_at_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    bundle = analysis("order-lookup")
    repository = FileAnalysisRepository(Path("var/analyses"))
    repository.save(bundle)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert repository.get(bundle.analysis_id) == bundle
