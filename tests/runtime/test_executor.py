import json
import shutil
from pathlib import Path

import pytest

from flowdelta.ingestion import InputError
from flowdelta.module_outputs import validate_runs
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.order_lookup import EXEC_OVERHEAD_SECONDS
from flowdelta.runtime.preflight import read_plan_bundle

from .fake_docker import FakeDocker

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "contracts" / "order-lookup"


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of the plan bundle and a minimal repository (runtime files + fixture sources)."""
    bundle = Path(shutil.copytree(EXAMPLE, tmp_path / "bundle"))
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "runtime" / "order-lookup", repo / "runtime" / "order-lookup")
    shutil.copytree(REPO / "src/flowdelta/baseline", repo / "src/flowdelta/baseline")
    return bundle, repo


def execute(
    workspace: tuple[Path, Path], docker: FakeDocker, name: str = "run-1", *, plan_update=None
):
    bundle, repo = workspace
    handoff, plan, catalog = read_plan_bundle(bundle)
    if plan_update:
        plan = plan.model_copy(update=plan_update)
    executor = PlanExecutor(bundle, repo, runner=docker, sleep=lambda seconds: None)
    output = repo / "var" / name
    return executor.execute_plan(handoff, plan, catalog, output), output, plan


def load(output: Path, relative: str) -> dict:
    return json.loads((output / relative).read_bytes())


def test_plan_runs_before_and_after_in_separate_projects(workspace) -> None:
    docker = FakeDocker()
    runs, output, plan = execute(workspace, docker)

    assert [(run.side, run.attempt_number) for run in runs] == [("before", 1), ("after", 1)]
    assert all(run.execution_status == "SUCCEEDED" for run in runs)
    assert all(run.cleanup_status == "COMPLETE" for run in runs)
    assert all(run.artifacts.collection_status == "COMPLETE" for run in runs)
    assert all(run.outcome is None for run in runs)
    assert all(run.authorship.origin == "RUNTIME_OBSERVATION" for run in runs)
    assert all(run.plan_id == plan.plan_id for run in runs)
    assert [run.snapshot_id for run in runs] == [plan.before_snapshot_id, plan.after_snapshot_id]
    # Separate identity and Compose project per side, one comparison ID for the pair.
    assert runs[0].run_id != runs[1].run_id
    assert len({run.extensions["flowdelta.compose_project"] for run in runs}) == 2
    assert len(docker.projects) == 2
    assert runs[0].comparison_run_id == runs[1].comparison_run_id

    # Files are written where the run records say, with bundle-root paths.
    for run, label in zip(runs, ["before-1", "after-1"], strict=True):
        assert (output / "runs" / f"{label}.run.json").is_file()
        assert all(ref.path.startswith(f"runs/{label}/") for ref in run.artifacts.artifacts)
        events = load(output, f"runs/{label}/events.json")["events"]
        assert [event["run_id"] for event in events] == [run.run_id]
    handoff, _, catalog = read_plan_bundle(workspace[0])
    validate_runs(handoff, catalog, plan, runs, output)


def test_request_uses_plan_input_and_time_limit(workspace) -> None:
    docker = FakeDocker()
    _, output, plan = execute(workspace, docker, plan_update={"timeout_seconds": 2})
    expected = (plan.normal_input, plan.timeout_seconds + EXEC_OVERHEAD_SECONDS)
    assert docker.requests == [expected, expected]
    assert docker.http_timeouts == [2, 2]
    request = load(output, "runs/before-1/request.json")
    assert request["body"] == plan.normal_input
    assert request["timeout_seconds"] == plan.timeout_seconds


def test_secrets_stay_out_of_shared_run_files(workspace) -> None:
    docker = FakeDocker()
    _, output, _ = execute(workspace, docker)
    secrets = [value for project in docker.projects.values() for value in project.secrets]
    assert len(secrets) >= 8
    shared = b"".join(path.read_bytes() for path in (output / "runs").rglob("*") if path.is_file())
    assert not any(secret.encode() in shared for secret in secrets)
    assert b"[REDACTED]" in (output / "private/before-1/services.log").read_bytes()
    assert all(path.suffix == ".json" for path in (output / "runs").rglob("*") if path.is_file())


def test_new_output_folder_gets_new_identity_and_clean_state(workspace) -> None:
    first, _, _ = execute(workspace, FakeDocker(), "run-1")
    second, output, _ = execute(workspace, FakeDocker(), "run-2")
    assert {run.run_id for run in first}.isdisjoint(run.run_id for run in second)
    initial = load(output, "runs/before-1/initial-state.json")
    assert initial["events"] == [] and initial["run_id"] == second[0].run_id
    with pytest.raises(InputError, match="^OUTPUT_ALREADY_EXISTS$"):
        execute(workspace, FakeDocker(), "run-1")


@pytest.mark.parametrize("failure", ["timeout", "http-timeout"])
def test_request_timeout_still_collects_and_cleans_up(workspace, failure: str) -> None:
    runs, output, _ = execute(workspace, FakeDocker(failures={"normal-request": failure}))
    for run in runs:
        assert run.execution_status == "TIMED_OUT"
        assert run.error_code == "REQUEST_TIMEOUT"
        assert run.extensions["flowdelta.failed_phase"] == "COLLECTING"
        assert run.cleanup_status == "COMPLETE"
        assert run.artifacts.collection_status == "PARTIAL"
        assert run.artifacts.missing_kinds == ["response"]
    # What happened during the timed-out request is still visible.
    assert len(load(output, "runs/after-1/events.json")["events"]) == 1
    assert load(output, "runs/after-1/error.json")["execution_attempted"] is True


def test_rejected_request_is_failed_not_succeeded(workspace) -> None:
    runs, _, _ = execute(workspace, FakeDocker(response_status=500))
    assert {run.execution_status for run in runs} == {"FAILED"}
    assert {run.error_code for run in runs} == {"NORMAL_REQUEST_REJECTED"}
    assert {run.cleanup_status for run in runs} == {"COMPLETE"}


def test_cleanup_failure_is_kept_separately(workspace) -> None:
    runs, output, _ = execute(workspace, FakeDocker(failures={"down": "fail"}))
    assert {run.execution_status for run in runs} == {"SUCCEEDED"}
    assert {run.cleanup_status for run in runs} == {"FAILED"}
    cleanup = load(output, "runs/before-1/cleanup.json")
    assert cleanup["status"] == "FAILED" and cleanup["error_code"] == "COMMAND_FAILED_CLEANUP"
    assert cleanup["check_command"].startswith("docker ps --all --filter label=")


def test_missing_image_is_not_run_and_never_starts_services(workspace) -> None:
    docker = FakeDocker(failures={"image": "fail"})
    runs, output, _ = execute(workspace, docker)
    for run in runs:
        assert run.execution_status == "NOT_RUN"
        assert run.error_code == "IMAGE_NOT_PREPARED"
        assert run.extensions["flowdelta.failed_phase"] == "PREPARING"
        assert run.artifacts.collection_status == "NOT_STARTED"
        assert run.cleanup_status == "COMPLETE"
    assert not any("up" in call for call in docker.calls)
    assert load(output, "runs/before-1/error.json")["execution_attempted"] is False


def test_preparation_timeout_is_not_reported_as_request_timeout(workspace) -> None:
    runs, _, _ = execute(workspace, FakeDocker(failures={"import-credentials": "timeout"}))
    assert {run.execution_status for run in runs} == {"NOT_RUN"}
    assert {run.error_code for run in runs} == {"TIMEOUT_IMPORT_CREDENTIALS"}


def test_event_from_another_run_is_reported(workspace) -> None:
    runs, _, _ = execute(workspace, FakeDocker(foreign_event=True))
    assert {run.error_code for run in runs} == {"EVENT_RUN_MISMATCH"}
    assert {run.execution_status for run in runs} == {"FAILED"}


def test_invalid_plan_starts_nothing_and_creates_no_output(workspace) -> None:
    bundle, repo = workspace
    handoff, plan, catalog = read_plan_bundle(bundle)
    docker = FakeDocker()
    executor = PlanExecutor(bundle, repo, runner=docker)
    with pytest.raises(InputError, match="^TEMPLATE_NOT_FOUND$"):
        executor.execute_plan(
            handoff, plan.model_copy(update={"template_id": "other"}), catalog, repo / "var/x"
        )
    assert docker.calls == []
    assert not (repo / "var" / "x").exists()


class StopAtRequest(FakeDocker):
    """Acts like Ctrl+C when the plan's request is sent to the order service."""

    def __call__(self, args, *, timeout, input_text=None):
        if input_text and '"operation": "normal-request"' in input_text:
            raise KeyboardInterrupt
        return super().__call__(args, timeout=timeout, input_text=input_text)


def test_stop_signal_is_recorded_as_interrupted_then_raised(workspace) -> None:
    bundle, repo = workspace
    handoff, plan, catalog = read_plan_bundle(bundle)
    executor = PlanExecutor(bundle, repo, runner=StopAtRequest(), sleep=lambda seconds: None)
    output = repo / "var" / "run-stop"
    with pytest.raises(KeyboardInterrupt):
        executor.execute_plan(handoff, plan, catalog, output)
    run = load(output, "runs/before-1.run.json")
    assert run["execution_status"] == "INTERRUPTED"
    assert run["error_code"] == "INTERRUPTED"
    assert run["cleanup_status"] == "COMPLETE"
    assert run["outcome"] is None
    # The stop ends the whole plan: the second side is not started.
    assert not (output / "runs" / "after-1.run.json").exists()


class StopAtCommand(FakeDocker):
    """Acts like Ctrl+C while a cleanup command runs (for example ``logs`` or ``down``)."""

    def __init__(self, word: str) -> None:
        super().__init__()
        self.word = word

    def __call__(self, args, *, timeout, input_text=None):
        if self.word in args:
            raise KeyboardInterrupt
        return super().__call__(args, timeout=timeout, input_text=input_text)


@pytest.mark.parametrize("word", ["logs", "down"])
def test_stop_during_cleanup_still_records_and_then_raises(workspace, word) -> None:
    bundle, repo = workspace
    handoff, plan, catalog = read_plan_bundle(bundle)
    executor = PlanExecutor(bundle, repo, runner=StopAtCommand(word), sleep=lambda seconds: None)
    output = repo / "var" / f"run-stop-{word}"
    with pytest.raises(KeyboardInterrupt):
        executor.execute_plan(handoff, plan, catalog, output)
    run = load(output, "runs/before-1.run.json")
    # 정리 중 중단은 기록을 건너뛰지 않는다. 정리 완료를 확인할 수 없으면 FAILED다.
    # logs 중단 뒤에도 down과 남은 자원 확인이 실행돼 정리는 완료된다. down 중단은 확인 불가다.
    expected = "COMPLETE" if word == "logs" else "FAILED"
    assert run["cleanup_status"] == expected
    assert (output / "runs" / "before-1.run.json").exists()
    assert not (output / "runs" / "after-1.run.json").exists()
