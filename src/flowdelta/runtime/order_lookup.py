"""Run one side (before or after) of the registered normal order lookup from a checked plan.

The flow follows the team lead's fixed baseline (`flowdelta.baseline.manual`) but every value
comes from `PreflightResult` and the plan, and each step records which phase it reached:

    PREPARING -> READY -> RUNNING -> COLLECTING -> FINALIZING -> FINISHED

Execution, collection and cleanup are recorded separately. Cleanup is always attempted and only
touches this run's Compose project. No security verdict is made here (`outcome` stays null).
"""

import json
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from flowdelta.contracts import ArtifactRef
from flowdelta.ingestion import digest, json_bytes
from flowdelta.pipeline_contracts import ArtifactCollection, Authorship, ExecutionRun, TestPlan
from flowdelta.runtime.preflight import PreflightResult, SideSource

PRODUCER = "flowdelta.runtime.order_lookup/0.1.1"
RUNTIME_WORKFLOW_ID = "fdOrderRuntime"
# docker exec start-up time on top of the plan's request time limit.
EXEC_OVERHEAD_SECONDS = 15
MAX_LOG_BYTES = 1_000_000
# The fixed observer inside the fixture image (source hash checked before use).
PROBE = ["/app/.venv/bin/python", "-m", "flowdelta.baseline.probe"]
# Required artifact kind -> file this runner writes for it.
KIND_FILES = {
    "initial-state": "initial-state.json",
    "response": "response.json",
    "final-state": "final-state.json",
    "events": "events.json",
}

Phase = Literal["QUEUED", "PREPARING", "READY", "RUNNING", "COLLECTING", "FINALIZING", "FINISHED"]
ExecutionStatus = Literal["NOT_RUN", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """One external command. Tests replace this so no real Docker is needed."""

    def __call__(
        self, args: list[str], *, timeout: float, input_text: str | None = None
    ) -> CommandResult: ...


def subprocess_runner(
    args: list[str], *, timeout: float, input_text: str | None = None
) -> CommandResult:
    result = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


class RunFailure(Exception):
    """A stable code for the run record; never carries secrets or raw command output."""

    def __init__(self, code: str, *, timed_out: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.timed_out = timed_out


@dataclass(frozen=True)
class SideRequest:
    checked: PreflightResult
    source: SideSource
    attempt: int
    plan: TestPlan
    case_id: str
    comparison_run_id: str
    repo: Path
    output: Path  # Final bundle root. Shared files go to runs/, secrets to private/.


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def run_side(
    request: SideRequest,
    runner: CommandRunner = subprocess_runner,
    sleep: Callable[[float], None] = time.sleep,
) -> ExecutionRun:
    checked, source, plan = request.checked, request.source, request.plan
    lock, registered = checked.lock, checked.registered
    label = f"{source.side}-{request.attempt}"

    # --- Allocation: a new identity, project, folders and credentials for this run only.
    run_id = f"run-{uuid4().hex}"
    project = f"fdrt-{uuid4().hex[:16]}"
    shared = request.output / "runs" / label
    private = request.output / "private" / label
    shared.mkdir(parents=True)
    private.mkdir(parents=True, mode=0o700)
    for folder in ["n8n", "fixture", "import"]:
        (private / folder).mkdir(mode=0o777)
        (private / folder).chmod(0o777 if folder != "import" else 0o755)
    credentials = {
        name: secrets.token_hex(32) for name in ["encryption", "client", "service", "control"]
    }
    env_file = private / ".env"
    env_file.write_text(
        f'FD_RUN_DIR="{private.as_posix()}"\nFD_RUN_ID={run_id}\n'
        f"FD_FIXTURE_IMAGE={registered.fixture_image}\n"
        f"FD_ENCRYPTION_KEY={credentials['encryption']}\n"
        f"FD_SERVICE_TOKEN={credentials['service']}\nFD_CONTROL_TOKEN={credentials['control']}\n",
        encoding="utf-8",
        newline="\n",
    )
    env_file.chmod(0o600)
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "--file",
        str(request.repo / registered.compose_path),
    ]
    label_filter = f"label=com.docker.compose.project={project}"
    prep_timeout = lock.preparation_timeout_seconds

    def redact(text: str) -> str:
        for value in credentials.values():
            text = text.replace(value, "[REDACTED]")
        return text

    def write_log(name: str, text: str) -> None:
        data = redact(text).encode("utf-8")[-MAX_LOG_BYTES:]
        # Diagnostic text is local-only; the common bundle accepts explicit JSON evidence.
        (private / f"{name}.log").write_bytes(data)

    def call(args: list[str], name: str, timeout: float, input_text: str | None = None) -> str:
        code = name.upper().replace("-", "_")
        try:
            result = runner(args, timeout=timeout, input_text=input_text)
        except subprocess.TimeoutExpired as exc:
            raise RunFailure(f"TIMEOUT_{code}", timed_out=True) from exc
        except OSError as exc:
            raise RunFailure("DOCKER_UNAVAILABLE") from exc
        if input_text is None:  # Probe input carries credentials; its exchange is not logged.
            write_log(name, result.stdout + result.stderr)
        if result.returncode:
            raise RunFailure(f"COMMAND_FAILED_{code}")
        return result.stdout.strip()

    def observe(
        operation: str,
        headers: dict[str, str] | None = None,
        body: object | None = None,
        timeout: float = 45,
    ) -> tuple[int, Any]:
        payload = json.dumps(
            {
                "operation": operation,
                "headers": headers or {},
                "body": body,
                "timeout_seconds": checked.timeout_seconds if operation == "normal-request" else 30,
            }
        )
        output = call(
            [*compose, "exec", "-T", "orders-api", *PROBE],
            f"probe-{operation}",
            timeout,
            input_text=payload,
        )
        try:
            value = json.loads(output)
        except ValueError as exc:
            raise RunFailure("OBSERVER_OUTPUT_INVALID") from exc
        if "transport_error" in value:
            raise RunFailure(
                "OBSERVER_TRANSPORT_ERROR", timed_out=value["transport_error"] == "TimeoutError"
            )
        return int(value["status_code"]), value["body"]

    def wait_ready(operation: str, deadline: float) -> None:
        while time.monotonic() < deadline:
            try:
                status, _ = observe(operation)
                if status == 200:
                    return
            except RunFailure:
                pass
            sleep(1.0)
        raise RunFailure("READINESS_TIMEOUT", timed_out=True)

    def state(name: str) -> dict[str, Any]:
        status, value = observe("state", {"x-flowdelta-control": credentials["control"]})
        if status != 200 or not isinstance(value, dict):
            raise RunFailure(f"{name}_UNAVAILABLE")
        return value

    phase: Phase = "QUEUED"
    execution_status: ExecutionStatus = "NOT_RUN"
    cleanup_status: Literal["PENDING", "COMPLETE", "FAILED"] = "PENDING"
    error_code: str | None = None
    failed_phase: Phase | None = None
    started_at: str | None = None
    actual_version: str | None = None
    actual_digest: str | None = None
    observed: list[str] = []
    # 중단 신호(Ctrl+C)는 INTERRUPTED로 기록한 뒤, 실행 기록을 쓰고 나서 다시 던진다.
    interrupt: BaseException | None = None
    # 정리 단계에서 중단된 지점. 실행 상태와 별개로 cleanup.json에 남긴다.
    cleanup_stop_step: str | None = None
    write_failure: Exception | None = None
    record: ExecutionRun | None = None

    def write_guarded(step: Callable[[], None]) -> None:
        # 기록 한 단계가 실패해도 다음 기록을 계속 쓴다. 중단은 최초 것을 보관한다.
        nonlocal interrupt, write_failure
        try:
            step()
        except Exception as exc:
            if write_failure is None:
                write_failure = exc
        except BaseException as exc:
            if interrupt is None:
                interrupt = exc

    save(
        shared / "allocation.json",
        {
            "schema_version": "0.4.0",
            "run_id": run_id,
            "project": project,
            "side": source.side,
            "attempt_number": request.attempt,
            "plan_id": plan.plan_id,
            "template_id": checked.template.template_id,
            "template_version": checked.template.version,
            "created_at": now(),
        },
    )
    try:
        # --- PREPARING: images must already exist; a run never downloads or builds them.
        phase = "PREPARING"
        for image in [lock.n8n_image, registered.fixture_image]:
            try:
                found = runner(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=60
                )
            except subprocess.TimeoutExpired as exc:
                raise RunFailure("TIMEOUT_IMAGE_CHECK", timed_out=True) from exc
            except OSError as exc:
                raise RunFailure("DOCKER_UNAVAILABLE") from exc
            if found.returncode:
                raise RunFailure("IMAGE_NOT_PREPARED")

        workflow = json.loads(source.workflow_path.read_bytes())
        workflow["id"] = RUNTIME_WORKFLOW_ID
        save(private / "import/workflow.json", workflow)
        (private / "import/initial.json").write_bytes(checked.initial_state_path.read_bytes())
        save(
            private / "import/credentials.json",
            [
                {
                    "id": "fd-client-auth",
                    "name": "FlowDelta local client",
                    "type": "httpHeaderAuth",
                    "data": {"name": "x-flowdelta-client", "value": credentials["client"]},
                },
                {
                    "id": "fd-service-auth",
                    "name": "FlowDelta order service",
                    "type": "httpHeaderAuth",
                    "data": {"name": "x-flowdelta-service", "value": credentials["service"]},
                },
            ],
        )
        save(
            shared / "binding.json",
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "source_snapshot_id": source.snapshot_id,
                "source_workflow_sha256": digest(source.workflow_path.read_bytes()),
                "runtime_workflow_sha256": digest((private / "import/workflow.json").read_bytes()),
                "runtime_workflow_id": RUNTIME_WORKFLOW_ID,
                "transformations": ["Set a fixed workflow ID for this isolated runtime."],
                "initial_state_sha256": plan.initial_state_ref.sha256,
                "credential_refs": ["fd-client-auth", "fd-service-auth"],
                "secret_values_in_artifacts": False,
            },
        )
        run_n8n = [*compose, "run", "--rm", "--no-deps", "n8n"]
        for name, args in [
            ("import-credentials", ["import:credentials", "--input=/import/credentials.json"]),
            ("import-workflow", ["import:workflow", "--input=/import/workflow.json"]),
            ("publish-workflow", ["publish:workflow", f"--id={RUNTIME_WORKFLOW_ID}"]),
        ]:
            call([*run_n8n, *args], name, prep_timeout)
        call([*compose, "up", "--detach", "--no-build"], "start-services", prep_timeout)
        deadline = time.monotonic() + prep_timeout
        wait_ready("n8n-health", deadline)
        wait_ready("fixture-health", deadline)

        _, metadata = observe("fixture-metadata")
        expected_sources = {
            name: digest((request.repo / "src/flowdelta/baseline" / name).read_bytes())
            for name in ["fixture.py", "probe.py"]
        }
        if metadata.get("source_sha256") != expected_sources:
            raise RunFailure("FIXTURE_REBUILD_REQUIRED")
        if (metadata.get("python"), metadata.get("fastapi"), metadata.get("uvicorn")) != (
            lock.python_version,
            lock.fastapi_version,
            lock.uvicorn_version,
        ):
            raise RunFailure("FIXTURE_VERSION_MISMATCH")
        actual_version = call(
            [*compose, "exec", "-T", "n8n", "n8n", "--version"], "engine-version", 60
        )
        if actual_version != lock.engine_version:
            raise RunFailure("ENGINE_VERSION_MISMATCH")
        container_id = call([*compose, "ps", "--quiet", "n8n"], "n8n-container", 60)
        actual_digest = call(
            ["docker", "inspect", "--format", "{{.Image}}", container_id], "n8n-image", 60
        )
        expected_image = call(
            ["docker", "image", "inspect", "--format", "{{.Id}}", lock.n8n_image],
            "expected-image",
            60,
        )
        if actual_digest != expected_image:
            raise RunFailure("ENGINE_IMAGE_MISMATCH")
        network_id = call(
            ["docker", "network", "ls", "--quiet", "--filter", label_filter], "network-id", 60
        )
        network = json.loads(
            call(["docker", "network", "inspect", network_id], "network-inspect", 60)
        )[0]
        ports = json.loads(
            call(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id],
                "n8n-ports",
                60,
            )
        )
        if not network.get("Internal") or any((ports or {}).values()):
            raise RunFailure("PRIVATE_NETWORK_REQUIRED")
        save(
            shared / "runtime.json",
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "project": project,
                "runtime_profile": lock.profile_id,
                "engine_version": actual_version,
                "image_id": actual_digest,
                "image_reference": lock.n8n_image,
                "network": "internal",
                "published_ports": ports,
                "observation_transport": "docker-exec-fixed-probe",
                "fixture_metadata": metadata,
            },
        )

        # --- READY: the service starts from the plan's initial state, with no events yet.
        phase = "READY"
        initial = state("INITIAL_STATE")
        save(shared / "initial-state.json", initial)
        observed.append("initial-state")
        if (
            initial.get("run_id") != run_id
            or initial.get("metadata", {}).get("initial_sha256") != plan.initial_state_ref.sha256
            or initial.get("events")
        ):
            raise RunFailure("INITIAL_STATE_MISMATCH")

        # --- RUNNING: one request with the plan's input and time limit. No automatic retry.
        phase = "RUNNING"
        save(
            shared / "request.json",
            {
                "method": registered.method,
                "path": registered.webhook_path,
                "body": plan.normal_input,
                "timeout_seconds": checked.timeout_seconds,
                "authentication": "local-credential-reference",
            },
        )
        started_at = now()
        request_failure: RunFailure | None = None
        status_code: int | None = None
        try:
            status_code, body = observe(
                "normal-request",
                {"x-flowdelta-client": credentials["client"]},
                plan.normal_input,
                timeout=checked.timeout_seconds + EXEC_OVERHEAD_SECONDS,
            )
            save(shared / "response.json", {"status_code": status_code, "body": body})
            observed.append("response")
        except RunFailure as exc:
            request_failure = (
                RunFailure("REQUEST_TIMEOUT", timed_out=True) if exc.timed_out else exc
            )

        # --- COLLECTING: still collect after a failed request, to see what did happen.
        phase = "COLLECTING"
        final = state("FINAL_STATE")
        save(shared / "final-state.json", final)
        observed.append("final-state")
        events = final.get("events", [])
        save(
            shared / "events.json",
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "events": events,
                "collection_status": "COMPLETE",
            },
        )
        observed.append("events")
        if final.get("run_id") != run_id or any(event.get("run_id") != run_id for event in events):
            raise RunFailure("EVENT_RUN_MISMATCH")
        if request_failure is not None:
            raise request_failure
        if status_code != 200:
            raise RunFailure("NORMAL_REQUEST_REJECTED")
        execution_status = "SUCCEEDED"
    except BaseException as exc:  # Every failure, including a stop signal, gets a run record.
        failed_phase = phase
        if not isinstance(exc, Exception):
            interrupt = exc
            error_code = "INTERRUPTED"
            execution_status = "INTERRUPTED" if started_at is not None else "NOT_RUN"
        else:
            error_code = exc.code if isinstance(exc, RunFailure) else type(exc).__name__
            timed_out = isinstance(exc, RunFailure) and exc.timed_out
            if started_at is None:
                execution_status = "NOT_RUN"
            else:
                execution_status = "TIMED_OUT" if timed_out else "FAILED"
    finally:
        # --- FINALIZING: remove only this project's containers, networks and volumes.
        phase = "FINALIZING"
        try:
            call([*compose, "logs", "--no-color"], "services", 60)
        except RunFailure:
            pass
        except Exception:
            # 로그 수집은 진단용이라 실패해도 정리와 기록을 계속한다.
            pass
        except BaseException as exc:
            # 로그 수집 중 중단돼도 정리와 기록은 계속하고, 중단은 마지막에 다시 던진다.
            if interrupt is None:
                interrupt = exc
            cleanup_stop_step = "services-logs"
        cleanup: dict[str, Any] = {"schema_version": "0.4.0", "run_id": run_id, "project": project}
        try:
            call([*compose, "down", "--volumes", "--remove-orphans"], "cleanup", prep_timeout)
            remaining = {
                kind: call(
                    ["docker", *command, "--quiet", "--filter", label_filter],
                    f"remaining-{kind}",
                    60,
                ).split()
                for kind, command in [
                    ("containers", ["ps", "--all"]),
                    ("networks", ["network", "ls"]),
                    ("volumes", ["volume", "ls"]),
                ]
            }
            cleanup_status = "COMPLETE" if not any(remaining.values()) else "FAILED"
            cleanup.update(status=cleanup_status, remaining=remaining)
        except RunFailure as exc:
            cleanup_status = "FAILED"
            cleanup.update(status="FAILED", error_code=exc.code)
        except Exception as exc:
            # 일반 오류(예: 로그 파일 쓰기 실패)는 중단이 아니므로 실제 원인을 그대로 남긴다.
            cleanup_status = "FAILED"
            cleanup.update(status="FAILED", error_code=type(exc).__name__)
        except BaseException as exc:
            # 정리 도중 중단되면 정리 완료를 확인할 수 없으므로 FAILED로 남긴다.
            if interrupt is None:
                interrupt = exc
            cleanup_stop_step = "cleanup"
            cleanup_status = "FAILED"
            cleanup.update(status="FAILED", error_code="INTERRUPTED")
        if cleanup_stop_step is not None:
            # 중단 단계와 코드를 실행 상태와 별도로 남겨 공유 증거만으로 사유를 알 수 있게 한다.
            cleanup["interrupted"] = {
                "phase": "FINALIZING",
                "step": cleanup_stop_step,
                "error_code": "INTERRUPTED",
            }
        cleanup["check_command"] = f"docker ps --all --filter {label_filter}"
        cleanup["private_local_files_retained"] = True
        write_guarded(lambda: save(shared / "cleanup.json", cleanup))

    phase = "FINISHED"
    if error_code is not None:
        write_guarded(
            lambda: save(
                shared / "error.json",
                {
                    "error_code": error_code,
                    "failed_phase": failed_phase,
                    "execution_attempted": started_at is not None,
                },
            )
        )

    def build_record() -> None:
        nonlocal record
        record = _record(
            request,
            run_id=run_id,
            project=project,
            shared=shared,
            observed=observed,
            execution_status=execution_status,
            cleanup_status=cleanup_status,
            error_code=error_code,
            failed_phase=failed_phase,
            started_at=started_at,
            actual_version=actual_version,
            actual_digest=actual_digest,
        )

    # 앞 단계 기록이 실패하거나 다시 중단돼도 run.json은 쓴다.
    write_guarded(build_record)
    if interrupt is not None:
        # 최초 중단을 그대로 다시 던진다. 기록 실패는 중단보다 뒤에 알린다.
        raise interrupt
    if write_failure is not None:
        raise write_failure
    if record is None:
        raise RuntimeError("RUN_RECORD_NOT_WRITTEN")
    return record


def _record(
    request: SideRequest,
    *,
    run_id: str,
    project: str,
    shared: Path,
    observed: list[str],
    execution_status: ExecutionStatus,
    cleanup_status: Literal["PENDING", "COMPLETE", "FAILED"],
    error_code: str | None,
    failed_phase: Phase | None,
    started_at: str | None,
    actual_version: str | None,
    actual_digest: str | None,
) -> ExecutionRun:
    """Link the files that really exist, by bundle-root path, and write runs/<side>-<n>.run.json."""
    label = shared.name
    refs = []
    for index, path in enumerate(sorted(shared.iterdir())):
        content = path.read_bytes()
        refs.append(
            ArtifactRef(
                artifact_id=f"{label}-{index}",
                path=f"runs/{label}/{path.name}",
                sha256=digest(content),
                size_bytes=len(content),
                kind="generated",
                media_type="application/json" if path.suffix == ".json" else "text/plain",
            )
        )
    by_name = {ref.path.rsplit("/", 1)[1]: ref.artifact_id for ref in refs}
    required = list(request.checked.template.required_artifact_kinds)
    kinds = {kind: by_name[KIND_FILES[kind]] for kind in observed}
    missing = sorted(set(required) - set(observed))
    extensions: dict[str, Any] = {"flowdelta.compose_project": project}
    if failed_phase is not None:
        extensions["flowdelta.failed_phase"] = failed_phase
    run = ExecutionRun(
        run_id=run_id,
        comparison_run_id=request.comparison_run_id,
        case_id=request.case_id,
        plan_id=request.plan.plan_id,
        side=request.source.side,
        attempt_number=request.attempt,
        snapshot_id=request.source.snapshot_id,
        authorship=Authorship(
            origin="RUNTIME_OBSERVATION",
            producer=PRODUCER,
            note="Plan-based run of a registered normal template; no security verdict.",
        ),
        runtime_profile=request.checked.lock.profile_id,
        actual_engine_version=actual_version,
        actual_image_digest=actual_digest,
        initial_state_sha256=request.plan.initial_state_ref.sha256,
        phase="FINISHED",
        execution_status=execution_status,
        cleanup_status=cleanup_status,
        started_at=started_at,
        finished_at=now(),
        error_code=error_code,
        extensions=extensions,
        artifacts=ArtifactCollection(
            run_id=run_id,
            artifacts=refs,
            artifact_kinds=kinds,
            required_kinds=required,
            observed_kinds=list(observed),
            missing_kinds=missing,
            collection_status=(
                "COMPLETE" if not missing else "PARTIAL" if observed else "NOT_STARTED"
            ),
        ),
    )
    save(request.output / "runs" / f"{label}.run.json", run.model_dump(mode="json"))
    return run
