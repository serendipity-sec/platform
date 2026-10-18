from collections.abc import Callable
from pathlib import Path

import pytest

from flowdelta.graph import analyze
from flowdelta.graph.repository import (
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


@pytest.mark.parametrize("analysis_id", ["..", ".", "a/b", ""])
def test_ids_that_could_leave_the_folder_are_refused(tmp_path: Path, analysis_id: str) -> None:
    with pytest.raises(InputError) as error:
        FileAnalysisRepository(tmp_path).get(analysis_id)
    assert error.value.code == "ANALYSIS_ID_INVALID"
