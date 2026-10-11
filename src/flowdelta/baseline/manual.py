"""Reproduce exactly the registered team-owned normal case. This is not C's plan executor."""

import json
import secrets
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from flowdelta.conformance.order import compare_order
from flowdelta.contracts import ArtifactRef
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, LoadedCase, digest, json_bytes, load_case
from flowdelta.pipeline_contracts import ArtifactCollection, Authorship, ExecutionRun


class BaselineError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def check_registered_case(repo: Path, loaded: LoadedCase) -> None:
    approved = json.loads((repo / "runtime/order-lookup/approved-inputs.json").read_bytes())
    for path, expected in approved["files"].items():
        if digest((repo / path).read_bytes()) != expected:
            raise BaselineError("REGISTERED_BASELINE_INPUT_CHANGED")
    if loaded.case.case_id != "order-lookup-demo-001":
        raise BaselineError("UNREGISTERED_BASELINE_CASE")
    if any(item.support != "SUPPORTED" for item in build_handoff(loaded).semantics.semantics):
        raise BaselineError("UNSUPPORTED_BASELINE_NODE")


def run_side(
    repo: Path,
    out: Path,
    loaded: LoadedCase,
    comparison_id: str,
    side: Literal["before", "after"],
    snapshot_id: str,
) -> ExecutionRun:
    run_id = f"run-{uuid4().hex}"
    project = f"fdbl-{uuid4().hex[:16]}"
    private = out / "private" / side
    artifacts = out / side
    private.mkdir(parents=True, mode=0o700)
    artifacts.mkdir()
    for folder in ["n8n", "fixture", "import"]:
        (private / folder).mkdir(mode=0o777)
        (private / folder).chmod(0o777 if folder != "import" else 0o755)
    credentials = {
        name: secrets.token_hex(32) for name in ["encryption", "client", "service", "control"]
    }
    env_file = private / ".env"
    env_file.write_text(
        f'FD_RUN_DIR="{private.as_posix()}"\nFD_RUN_ID={run_id}\n'
        f"FD_ENCRYPTION_KEY={credentials['encryption']}\n"
        f"FD_SERVICE_TOKEN={credentials['service']}\nFD_CONTROL_TOKEN={credentials['control']}\n",
        encoding="utf-8",
        newline="\n",
    )
    env_file.chmod(0o600)
    cli = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "--file",
        str(repo / "runtime/order-lookup/compose.yaml"),
    ]
    lock = tomllib.loads(
        (repo / "runtime/order-lookup/runtime.lock.toml").read_text(encoding="utf-8")
    )

    def command(args: list[str], label: str, timeout: int = 180) -> str:
        result = subprocess.run(
            [*cli, *args],
            cwd=repo,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout + result.stderr
        for value in credentials.values():
            output = output.replace(value, "[REDACTED]")
        (artifacts / f"{label}.log").write_text(output, encoding="utf-8", newline="\n")
        if result.returncode:
            raise BaselineError(f"COMMAND_FAILED_{label.upper().replace('-', '_')}")
        return result.stdout.strip()

    def observe(
        operation: str, headers: dict[str, str] | None = None, body: object | None = None
    ) -> tuple[int, Any]:
        result = subprocess.run(
            [
                *cli,
                "exec",
                "-T",
                "orders-api",
                "/app/.venv/bin/python",
                "-m",
                "flowdelta.baseline.probe",
            ],
            input=json.dumps({"operation": operation, "headers": headers or {}, "body": body}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise BaselineError("OBSERVER_UNAVAILABLE")
        value = json.loads(result.stdout)
        if "transport_error" in value:
            raise BaselineError("OBSERVER_TRANSPORT_ERROR")
        return int(value["status_code"]), value["body"]

    def wait_ready(operation: str) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                status, _ = observe(operation)
                if status == 200:
                    return
            except (OSError, ValueError, BaselineError, subprocess.SubprocessError):
                pass
            time.sleep(1)
        raise BaselineError("READINESS_TIMEOUT")

    save(
        artifacts / "allocation.json",
        {
            "schema_version": "0.4.0",
            "run_id": run_id,
            "project": project,
            "created_at": now(),
            "scope": "team-owned-normal-order-baseline",
        },
    )

    source_path = loaded.case.before_ref if side == "before" else loaded.case.after_ref
    workflow = json.loads(loaded.raw[source_path])
    workflow["id"] = "fdOrderBaseline"
    save(private / "import/workflow.json", workflow)
    (private / "import/initial.json").write_bytes(loaded.raw[loaded.case.initial_state_ref])
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
        artifacts / "binding.json",
        {
            "schema_version": "0.4.0",
            "run_id": run_id,
            "source_workflow_sha256": digest(loaded.raw[source_path]),
            "runtime_workflow_sha256": digest((private / "import/workflow.json").read_bytes()),
            "source_snapshot_id": snapshot_id,
            "runtime_workflow_id": "fdOrderBaseline",
            "transformations": ["Added fixed workflow ID for this isolated runtime."],
            "credential_refs": ["fd-client-auth", "fd-service-auth"],
            "secret_values_in_artifacts": False,
        },
    )
    started_at = None
    actual_version = None
    actual_digest = None
    execution_status: Literal["NOT_RUN", "SUCCEEDED", "FAILED", "TIMED_OUT"] = "NOT_RUN"
    cleanup_status: Literal["COMPLETE", "FAILED"] = "FAILED"
    error_code = None
    observed: list[str] = []
    try:
        command(
            [
                "run",
                "--rm",
                "--no-deps",
                "n8n",
                "import:credentials",
                "--input=/import/credentials.json",
            ],
            "import-credentials",
        )
        command(
            ["run", "--rm", "--no-deps", "n8n", "import:workflow", "--input=/import/workflow.json"],
            "import-workflow",
        )
        command(
            ["run", "--rm", "--no-deps", "n8n", "publish:workflow", "--id=fdOrderBaseline"],
            "publish-workflow",
        )
        command(["up", "--detach", "--no-build"], "start-services")
        wait_ready("n8n-health")
        wait_ready("fixture-health")
        _, fixture_metadata = observe("fixture-metadata")
        expected_sources = {
            name: digest((repo / "src/flowdelta/baseline" / name).read_bytes())
            for name in ["fixture.py", "probe.py"]
        }
        if fixture_metadata["source_sha256"] != expected_sources:
            raise BaselineError("FIXTURE_REBUILD_REQUIRED")
        for name in ["python", "fastapi", "uvicorn"]:
            if fixture_metadata[name] != lock[f"{name}_version"]:
                raise BaselineError("FIXTURE_VERSION_MISMATCH")
        actual_version = command(["exec", "-T", "n8n", "n8n", "--version"], "engine-version")
        if actual_version != lock["engine_version"]:
            raise BaselineError("ENGINE_VERSION_MISMATCH")
        # Compare the running image ID with the digest-pinned image, then save it.
        container_id = command(["ps", "--quiet", "n8n"], "n8n-container")
        actual_digest = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", container_id],
            text=True,
            encoding="utf-8",
        ).strip()
        expected_image_id = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", lock["n8n_image"]],
            text=True,
            encoding="utf-8",
        ).strip()
        if actual_digest != expected_image_id:
            raise BaselineError("ENGINE_IMAGE_MISMATCH")
        network_id = subprocess.check_output(
            [
                "docker",
                "network",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            text=True,
        ).strip()
        network = json.loads(
            subprocess.check_output(["docker", "network", "inspect", network_id], text=True)
        )[0]
        ports = json.loads(
            subprocess.check_output(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id],
                text=True,
            )
        )
        if not network["Internal"] or any(ports.values()):
            raise BaselineError("PRIVATE_NETWORK_REQUIRED")
        save(
            artifacts / "runtime.json",
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "project": project,
                "engine_version": actual_version,
                "image_id": actual_digest,
                "image_reference": lock["n8n_image"],
                "network": "internal",
                "published_ports": ports,
                "observation_transport": "docker-exec-fixed-probe",
                "fixture_metadata": fixture_metadata,
            },
        )
        initial_status, initial = observe("state", {"x-flowdelta-control": credentials["control"]})
        if initial_status != 200:
            raise BaselineError("INITIAL_STATE_UNAVAILABLE")
        save(artifacts / "initial-state.json", initial)
        observed.append("initial-state")
        if (
            initial["metadata"]["initial_sha256"]
            != digest(loaded.raw[loaded.case.initial_state_ref])
            or initial["events"]
        ):
            raise BaselineError("INITIAL_STATE_MISMATCH")
        save(
            artifacts / "request.json",
            {
                "method": "POST",
                "path": "/webhook/flowdelta-order-lookup",
                "body": loaded.task.normal_input,
                "authentication": "local-credential-reference",
            },
        )
        started_at = now()
        status, body = observe(
            "normal-request",
            {"x-flowdelta-client": credentials["client"]},
            loaded.task.normal_input,
        )
        save(artifacts / "response.json", {"status_code": status, "body": body})
        observed.append("response")
        final_status, final_state = observe(
            "state", {"x-flowdelta-control": credentials["control"]}
        )
        if final_status != 200:
            raise BaselineError("FINAL_STATE_UNAVAILABLE")
        save(artifacts / "final-state.json", final_state)
        save(
            artifacts / "events.json",
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "events": final_state["events"],
                "collection_status": "COMPLETE",
            },
        )
        observed.extend(["final-state", "events"])
        expected = json.loads((repo / "runtime/order-lookup/expected.json").read_bytes())
        if status != 200 or body != expected[f"{side}_response"]:
            raise BaselineError("NORMAL_RESPONSE_MISMATCH")
        if (
            initial["orders_sha256"] != final_state["orders_sha256"]
            or len(final_state["events"]) != 1
        ):
            raise BaselineError("NORMAL_STATE_OR_EVENT_MISMATCH")
        execution_status = "SUCCEEDED"
    except Exception as exc:
        # Preserve a failed run even for an unexpected preparation/transport exception.
        execution_status = "FAILED" if started_at else "NOT_RUN"
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) and started_at:
            execution_status = "TIMED_OUT"
        error_code = str(exc) if isinstance(exc, BaselineError) else type(exc).__name__
        save(
            artifacts / "error.json",
            {"error_code": error_code, "execution_attempted": started_at is not None},
        )
    finally:
        try:
            command(["logs", "--no-color"], "services")
        except (BaselineError, OSError, subprocess.SubprocessError):
            pass
        try:
            command(["down", "--volumes", "--remove-orphans"], "cleanup")
            containers = subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                text=True,
            ).split()
            networks = subprocess.check_output(
                [
                    "docker",
                    "network",
                    "ls",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                text=True,
            ).split()
            cleanup_status = "COMPLETE" if not containers and not networks else "FAILED"
            save(
                artifacts / "cleanup.json",
                {
                    "schema_version": "0.4.0",
                    "run_id": run_id,
                    "project": project,
                    "status": cleanup_status,
                    "remaining_container_ids": containers,
                    "remaining_network_ids": networks,
                    "private_local_files_retained": True,
                },
            )
        except (BaselineError, OSError, subprocess.SubprocessError):
            cleanup_status = "FAILED"
            save(
                artifacts / "cleanup.json",
                {
                    "schema_version": "0.4.0",
                    "run_id": run_id,
                    "project": project,
                    "status": "FAILED",
                },
            )
    if started_at and cleanup_status == "COMPLETE":
        try:
            comparison = compare_order(loaded, side, private, artifacts)
            observed.extend(["node-observations", "node-comparison"])
            if not comparison["all_observed_checks_match"]:
                error_code = "NODE_CONFORMANCE_MISMATCH"
        except (InputError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            error_code = "NODE_OBSERVATION_FAILED"
            save(
                artifacts / "node-observation-error.json",
                {"error_code": error_code, "exception_type": type(exc).__name__},
            )
    refs = []
    for index, path in enumerate(sorted(artifacts.iterdir())):
        content = path.read_bytes()
        refs.append(
            ArtifactRef(
                artifact_id=f"artifact-{index}",
                path=path.name,
                sha256=digest(content),
                size_bytes=len(content),
                kind="generated",
                media_type="application/json" if path.suffix == ".json" else "text/plain",
            )
        )
    required = [
        "response",
        "initial-state",
        "final-state",
        "events",
        "node-observations",
        "node-comparison",
    ]
    missing = sorted(set(required) - set(observed))
    artifact_kinds = {
        kind: next(ref.artifact_id for ref in refs if ref.path == f"{kind}.json")
        for kind in observed
    }
    run = ExecutionRun(
        run_id=run_id,
        comparison_run_id=comparison_id,
        case_id=loaded.case.case_id,
        plan_id="manual-normal-order-lookup",
        side=side,
        snapshot_id=snapshot_id,
        authorship=Authorship(
            origin="RUNTIME_OBSERVATION",
            producer="flowdelta.baseline.manual",
            note="Fixed normal baseline; no general plan executor or security verdict.",
        ),
        runtime_profile=lock["profile_id"],
        actual_engine_version=actual_version,
        actual_image_digest=actual_digest,
        initial_state_sha256=digest(loaded.raw[loaded.case.initial_state_ref]),
        phase="FINISHED",
        execution_status=execution_status,
        cleanup_status=cleanup_status,
        started_at=started_at,
        finished_at=now(),
        error_code=error_code,
        artifacts=ArtifactCollection(
            run_id=run_id,
            artifacts=refs,
            artifact_kinds=artifact_kinds,
            required_kinds=required,
            observed_kinds=observed,
            missing_kinds=missing,
            collection_status="PARTIAL" if missing else "COMPLETE",
        ),
    )
    save(artifacts / "run.json", run.model_dump(mode="json"))
    return run


def run_baseline(repo: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    loaded = load_case(repo / "examples/order-lookup")
    check_registered_case(repo, loaded)
    target = output.resolve()
    if target.exists():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    if not target.is_relative_to(repo / "var"):
        raise InputError("BASELINE_OUTPUT_MUST_BE_UNDER_VAR")
    target.mkdir(parents=True)
    handoff = build_handoff(loaded)
    write_handoff(loaded, handoff, target / "input")
    comparison_id = f"comparison-{uuid4().hex}"
    runs = []
    for snapshot in handoff.snapshots.snapshots:
        print(f"baseline {snapshot.side}: preparing isolated runtime", flush=True)
        run = run_side(repo, target, loaded, comparison_id, snapshot.side, snapshot.snapshot_id)
        runs.append(run)
        print(
            f"baseline {snapshot.side}: {run.execution_status}; cleanup {run.cleanup_status}",
            flush=True,
        )
        if (
            run.execution_status != "SUCCEEDED"
            or run.cleanup_status != "COMPLETE"
            or run.error_code
        ):
            break
    complete = len(runs) == 2 and all(
        item.execution_status == "SUCCEEDED"
        and item.cleanup_status == "COMPLETE"
        and item.error_code is None
        and item.artifacts.collection_status == "COMPLETE"
        for item in runs
    )
    result = {
        "schema_version": "0.4.0",
        "comparison_run_id": comparison_id,
        "authorship": "RUNTIME_OBSERVATION",
        "normal_baseline_complete": complete,
        "security_outcome": None,
        "runs": [
            {
                "run_id": item.run_id,
                "side": item.side,
                "status": item.execution_status,
                "cleanup": item.cleanup_status,
                "error_code": item.error_code,
                "collection_status": item.artifacts.collection_status,
            }
            for item in runs
        ],
    }
    save(target / "baseline.json", result)
    return result
