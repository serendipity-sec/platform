"""Run only hash-registered normal fixtures in disposable, networkless n8n containers."""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from flowdelta.ingestion import InputError, digest, json_bytes


def run_conformance(repo: Path, output: Path, *, only: str | None = None) -> dict[str, object]:
    repo = repo.resolve(strict=True)
    output = output.absolute()
    var = repo / "var"
    var.mkdir(exist_ok=True)
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    if not output.is_relative_to(var) or output == var:
        raise InputError("OUTPUT_MUST_BE_WITHIN_REPOSITORY_VAR")
    if output.exists() or output.is_symlink():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    registry = json.loads((repo / "runtime/conformance/registry.json").read_bytes())
    for relative, expected in registry["files"].items():
        if digest((repo / relative).read_bytes()) != expected:
            raise InputError("REGISTERED_CONFORMANCE_INPUT_CHANGED")
    cases = [item for item in registry["cases"] if only is None or item["case_id"] == only]
    if not cases:
        raise InputError("CONFORMANCE_CASE_NOT_REGISTERED")
    output.mkdir()
    runs = []
    image = registry["image"]
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if inspected.returncode:
            subprocess.run(["docker", "pull", image], check=True, capture_output=True, timeout=300)
        actual_image = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True, timeout=30
        ).strip()
        actual_version = subprocess.check_output(
            ["docker", "run", "--rm", "--network", "none", image, "--version"],
            text=True,
            timeout=90,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputError("CONFORMANCE_ENGINE_PREPARATION_FAILED") from exc
    if actual_version != registry["engine_version"]:
        raise InputError("ENGINE_VERSION_MISMATCH")
    for case in cases:
        print(f"conformance {case['case_id']}: preparing isolated runtime", flush=True)
        run_id = "run-" + uuid4().hex
        folder = output / case["case_id"]
        folder.mkdir()
        private = folder / "private"
        private.mkdir()
        state = private / "state"
        imports = private / "imports"
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        imports.mkdir()
        paths = [*registry["dependencies"], case["workflow"]]
        for index, relative in enumerate(paths):
            (imports / f"{index}.json").write_bytes((repo / relative).read_bytes())
        record: dict[str, object] = {
            "run_id": run_id,
            "case_id": case["case_id"],
            "started_at": datetime.now(UTC).isoformat(),
            "authorship": "RUNTIME_OBSERVATION",
            "network": "none",
            "image": image,
            "actual_image_id": actual_image,
            "actual_engine_version": actual_version,
            "expect_success": case["expect_success"],
            "source_sha256": registry["files"][case["workflow"]],
            "execution_status": "NOT_RUN",
            "cleanup_status": "PENDING",
            "outcome": None,
        }
        allocation = folder / "allocation.json"
        allocation.write_bytes(json_bytes(record))
        container = "fdconf-" + uuid4().hex[:16]
        base = [
            "docker",
            "run",
            "--rm",
            "--label",
            f"flowdelta.run={run_id}",
            "--network",
            "none",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--mount",
            f"type=bind,source={state},target=/home/node/.n8n",
            "--mount",
            f"type=bind,source={imports},target=/fixtures,readonly",
            "--env",
            "N8N_DIAGNOSTICS_ENABLED=false",
            "--env",
            "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
            "--env",
            "EXECUTIONS_DATA_SAVE_ON_SUCCESS=all",
            "--env",
            "EXECUTIONS_DATA_SAVE_ON_ERROR=all",
            image,
        ]
        try:
            for index, _ in enumerate(paths):
                result = subprocess.run(
                    [*base, "import:workflow", f"--input=/fixtures/{index}.json"],
                    capture_output=True,
                    timeout=150,
                    check=False,
                )
                (folder / f"import-{index}.log").write_bytes(result.stdout + result.stderr)
                if result.returncode:
                    raise InputError("CONFORMANCE_IMPORT_FAILED")
            # n8n 2.x executes a sub-workflow's published version. Publish only these local IDs.
            for index, _ in enumerate(paths):
                workflow_id = json.loads((imports / f"{index}.json").read_bytes())["id"]
                published = subprocess.run(
                    [*base, "publish:workflow", f"--id={workflow_id}"],
                    capture_output=True,
                    timeout=150,
                    check=False,
                )
                (folder / f"publish-{index}.log").write_bytes(published.stdout + published.stderr)
                if published.returncode:
                    raise InputError("CONFORMANCE_PUBLISH_FAILED")
            # The selected 2.42.6 image finalizes publication state during server startup.
            started = subprocess.run(
                [*base[:-1], "--name", container, "--detach", image, "start"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if started.returncode:
                raise InputError("CONFORMANCE_BOOTSTRAP_FAILED")
            ready = False
            for _ in range(60):
                health = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "node",
                        "-e",
                        "fetch('http://127.0.0.1:5678/healthz/readiness').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if health.returncode == 0:
                    ready = True
                    break
                time.sleep(1)
            logs = subprocess.run(["docker", "logs", container], capture_output=True, check=False)
            (folder / "bootstrap.log").write_bytes(logs.stdout + logs.stderr)
            subprocess.run(
                ["docker", "stop", "--timeout", "20", container], capture_output=True, check=True
            )
            if not ready:
                raise InputError("CONFORMANCE_BOOTSTRAP_TIMEOUT")
            result = subprocess.run(
                [*base, "execute", f"--id={case['workflow_id']}", "--rawOutput"],
                capture_output=True,
                timeout=150,
                check=False,
            )
            (folder / "stdout.log").write_bytes(result.stdout)
            (folder / "stderr.log").write_bytes(result.stderr)
            record["return_code"] = result.returncode
            record["execution_status"] = "SUCCEEDED" if result.returncode == 0 else "FAILED"
        except (OSError, subprocess.SubprocessError, InputError) as exc:
            record["error_code"] = exc.code if isinstance(exc, InputError) else type(exc).__name__
        finally:
            # --rm may already be deleting a stopped container. Judge the final inventory,
            # not a racing rm command's exit code. One-shot containers have unique names.
            for _ in range(30):
                remaining = subprocess.check_output(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label=flowdelta.run={run_id}",
                    ],
                    text=True,
                    timeout=20,
                ).split()
                if not remaining:
                    break
                subprocess.run(
                    ["docker", "rm", "--force", *remaining],
                    check=False,
                    capture_output=True,
                    timeout=20,
                )
                time.sleep(0.5)
            remaining = subprocess.check_output(
                ["docker", "ps", "--all", "--quiet", "--filter", f"label=flowdelta.run={run_id}"],
                text=True,
            ).split()
            record["cleanup_status"] = "COMPLETE" if not remaining else "FAILED"
            record["finished_at"] = datetime.now(UTC).isoformat()
            (folder / "run.json").write_bytes(json_bytes(record))
        from flowdelta.conformance.observations import collect_database

        database = state / "database.sqlite"
        if database.is_file():
            collected = collect_database(database, folder / "runtime-executions.json")
            record["observed_executions"] = len(collected["executions"])
            record["observed_node_ports"] = len(collected["observations"])
        record["expected_execution_observed"] = record["execution_status"] == (
            "SUCCEEDED" if case["expect_success"] else "FAILED"
        )
        (folder / "run.json").write_bytes(json_bytes(record))
        runs.append(record)
        print(
            f"conformance {case['case_id']}: {record['execution_status']}; "
            f"cleanup {record['cleanup_status']}; error {record.get('error_code')}",
            flush=True,
        )
    summary: dict[str, object] = {"scope": "registered-normal-node-conformance", "runs": runs}
    (output / "runs.json").write_bytes(json_bytes(summary))
    return summary
