import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from flowdelta.ingestion import InputError
from flowdelta.runtime.static_examples import example_runs
from flowdelta.runtime.static_scan import StaticScanRun

REPO = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    not DATABASE_URL, reason="set FLOWDELTA_KB_TEST_DATABASE_URL for PostgreSQL tests"
)


def unique(run: StaticScanRun, label: str) -> StaticScanRun:
    # 행은 변경할 수 없고 테스트 DB는 남으므로, 테스트마다 새 ID를 쓴다.
    token = uuid.uuid4().hex[:12]
    return run.model_copy(
        update={"run_id": f"static-{label}-{token}", "plan_id": f"plan-{label}-{token}"}
    )


def test_run_payload_round_trips_without_a_database() -> None:
    from flowdelta.runtime.static_database import decode_run, encode_run

    before, _ = example_runs(REPO)
    assert decode_run(encode_run(before), before.run_id) == before


def test_payload_for_another_run_id_is_refused() -> None:
    from flowdelta.runtime.static_database import decode_run, encode_run

    before, _ = example_runs(REPO)
    with pytest.raises(InputError, match="STATIC_RUN_INTEGRITY_MISMATCH"):
        decode_run(encode_run(before), "static-run-other")


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    if not DATABASE_URL:
        pytest.skip("set FLOWDELTA_KB_TEST_DATABASE_URL for PostgreSQL tests")
    pytest.importorskip("sqlalchemy")
    from pydantic import SecretStr

    from flowdelta.knowledge.database import engine_for
    from flowdelta.knowledge.settings import KnowledgeSettings
    from flowdelta.runtime.static_database import migrate_static_runs

    migration = os.environ.get("FLOWDELTA_KB_TEST_MIGRATION_URL", DATABASE_URL)
    settings = KnowledgeSettings(
        database_url=SecretStr(DATABASE_URL), migration_url=SecretStr(migration)
    )
    migrate_static_runs(settings)
    migrate_static_runs(settings)  # 같은 head를 다시 적용해도 변화가 없어야 한다
    app_engine = engine_for(settings)
    yield app_engine
    app_engine.dispose()


def repository(engine: object) -> object:
    from flowdelta.runtime.static_database import DatabaseStaticRunRepository

    return DatabaseStaticRunRepository(engine)  # type: ignore[arg-type]


@needs_database
def test_saved_run_is_read_back_exactly(engine: object) -> None:
    before, _ = example_runs(REPO)
    run = unique(before, "round")
    store = repository(engine)
    store.save(run)  # type: ignore[attr-defined]
    assert store.get(run.run_id) == run  # type: ignore[attr-defined]


@needs_database
def test_saving_the_same_run_twice_is_allowed_but_a_different_one_is_not(engine: object) -> None:
    before, _ = example_runs(REPO)
    run = unique(before, "again")
    store = repository(engine)
    store.save(run)  # type: ignore[attr-defined]
    store.save(run)  # type: ignore[attr-defined]
    changed = run.model_copy(
        update={
            "status": "FAILED",
            "error_code": "STATIC_TOOL_ERROR",
            "findings": [],
            "raw_report": None,
        }
    )
    with pytest.raises(InputError, match="STATIC_RUN_ID_CONFLICT"):
        store.save(changed)  # type: ignore[attr-defined]


@needs_database
def test_reruns_of_one_plan_are_listed_by_plan_and_never_mixed(engine: object) -> None:
    before, after = example_runs(REPO)
    first = unique(before, "rerun")
    second = first.model_copy(update={"run_id": first.run_id + "-2"})
    other = unique(after, "other")
    store = repository(engine)
    for run in (first, second, other):
        store.save(run)  # type: ignore[attr-defined]
    listed = store.list_by_plan(first.plan_id)  # type: ignore[attr-defined]
    assert sorted(run.run_id for run in listed) == sorted([first.run_id, second.run_id])
    assert all(run.plan_id == first.plan_id for run in listed)


@needs_database
def test_missing_run_is_reported_as_not_found(engine: object) -> None:
    with pytest.raises(InputError, match="STATIC_RUN_NOT_FOUND"):
        repository(engine).get("static-run-missing-" + uuid.uuid4().hex[:8])  # type: ignore[attr-defined]


@needs_database
def test_failed_run_is_stored_with_its_status_and_error(engine: object) -> None:
    before, _ = example_runs(REPO)
    failed = unique(before, "failed").model_copy(
        update={
            "status": "FAILED",
            "error_code": "STATIC_TOOL_ERROR",
            "findings": [],
            "raw_report": None,
            "started_at": None,
            "finished_at": None,
        }
    )
    store = repository(engine)
    store.save(failed)  # type: ignore[attr-defined]
    stored = store.get(failed.run_id)  # type: ignore[attr-defined]
    assert (stored.status, stored.error_code, stored.findings) == (
        "FAILED",
        "STATIC_TOOL_ERROR",
        [],
    )


@needs_database
def test_listing_can_be_limited_to_one_comparison_run(engine: object) -> None:
    before, after = example_runs(REPO)
    token = uuid.uuid4().hex[:12]
    plan = f"plan-group-{token}"
    first = before.model_copy(
        update={
            "run_id": f"static-g1-{token}",
            "plan_id": plan,
            "comparison_run_id": f"cmp-a-{token}",
        }
    )
    second = after.model_copy(
        update={
            "run_id": f"static-g2-{token}",
            "plan_id": plan,
            "comparison_run_id": f"cmp-b-{token}",
        }
    )
    store = repository(engine)
    store.save(first)  # type: ignore[attr-defined]
    store.save(second)  # type: ignore[attr-defined]
    only_first = store.list_by_plan(plan, f"cmp-a-{token}")  # type: ignore[attr-defined]
    assert [run.run_id for run in only_first] == [first.run_id]
    assert len(store.list_by_plan(plan)) == 2  # type: ignore[attr-defined]
