"""Run every ownership variant against every input on a real, isolated n8n (#28 demo).

One run = one fresh Compose project (n8n 2.42.6 pinned by digest + the experiment fixture) on an
internal network, fresh credentials, the registered initial state, one webhook request through
the fixture's fixed probe, then state collection and removal of the project.

The run records only what happened: the request, the HTTP response the client saw, and the
service's own state and events. It makes no security verdict (see judge_family.py).

`execution_status` is SUCCEEDED when the request got an HTTP answer (any status) and the final
state was collected; an error answer to an attack input is a completed run, not a failure.

Requires Docker and the images:
    docker build -f experiments/ownership-family/fixture.Dockerfile \
        -t flowdelta-ownership-fixture:0.1.0 .
Run from the repository root:
    uv run --frozen --extra baseline python experiments/ownership-family/run_family.py
"""

import json
import secrets
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowdelta.ingestion import digest, json_bytes

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
COMPOSE = HERE / "compose.yaml"
INITIAL = REPO / "examples/order-lookup/state/initial.json"
LOCK = REPO / "runtime/order-lookup/runtime.lock.toml"
WORK = REPO / "var/ownership-family"  # private, never committed (credentials and logs)
EVIDENCE = HERE / "evidence/runs"
FIXTURE_IMAGE = "flowdelta-ownership-fixture:0.1.0"
WORKFLOW_ID = "fdOrderRuntime"
PROBE = ["/app/.venv/bin/python", "-m", "flowdelta.baseline.probe"]
WEBHOOK = "/webhook/flowdelta-order-lookup"


class RunError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def artifact(content: object) -> dict[str, Any]:
    data = json_bytes(content)
    return {"content": content, "sha256": digest(data), "size_bytes": len(data)}


def run_one(variant: str, input_name: str, body: dict[str, str]) -> dict[str, Any]:
    run_id = f"run-{uuid4().hex}"
    project = f"fdof-{uuid4().hex[:16]}"
    private = WORK / f"{variant}__{input_name}"
    for folder in ["n8n", "fixture", "import"]:
        (private / folder).mkdir(parents=True, exist_ok=False)
        (private / folder).chmod(0o777)
    tokens = {
        name: secrets.token_hex(32) for name in ["encryption", "client", "service", "control"]
    }
    env_file = private / ".env"
    env_file.write_text(
        f'FD_RUN_DIR="{private.as_posix()}"\nFD_RUN_ID={run_id}\n'
        f"FD_ENCRYPTION_KEY={tokens['encryption']}\n"
        f"FD_SERVICE_TOKEN={tokens['service']}\nFD_CONTROL_TOKEN={tokens['control']}\n",
        encoding="utf-8",
        newline="\n",
    )
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "--file",
        str(COMPOSE),
    ]
    log: list[str] = []

    def call(args: list[str], name: str, timeout: float = 180, stdin: str | None = None) -> str:
        result = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        text = result.stdout + result.stderr
        for value in tokens.values():
            text = text.replace(value, "[REDACTED]")
        if stdin is None:
            log.append(f"## {name} (exit {result.returncode})\n{text[-4000:]}")
        if result.returncode:
            raise RunError(f"COMMAND_FAILED_{name.upper().replace('-', '_')}")
        return result.stdout.strip()

    def probe(operation: str, headers: dict[str, str] | None = None, data: object = None):
        payload = json.dumps(
            {"operation": operation, "headers": headers or {}, "body": data, "timeout_seconds": 30}
        )
        out = call(
            [*compose, "exec", "-T", "orders-api", *PROBE],
            f"probe-{operation}",
            timeout=60,
            stdin=payload,
        )
        value = json.loads(out)
        if "transport_error" in value:
            raise RunError(f"TRANSPORT_{value['transport_error']}")
        return int(value["status_code"]), value["body"]

    def wait(operation: str) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if probe(operation)[0] == 200:
                    return
            except (RunError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(1.0)
        raise RunError("READINESS_TIMEOUT")

    def state() -> dict[str, Any]:
        status, value = probe("state", {"x-flowdelta-control": tokens["control"]})
        if status != 200 or not isinstance(value, dict):
            raise RunError("STATE_UNAVAILABLE")
        return value

    workflow = json.loads((HERE / "workflows" / f"{variant}.json").read_bytes())
    workflow["id"] = WORKFLOW_ID
    (private / "import/workflow.json").write_bytes(json_bytes(workflow))
    (private / "import/initial.json").write_bytes(INITIAL.read_bytes())
    credentials = [
        {
            "id": "fd-client-auth",
            "name": "FlowDelta local client",
            "type": "httpHeaderAuth",
            "data": {"name": "x-flowdelta-client", "value": tokens["client"]},
        },
        {
            "id": "fd-service-auth",
            "name": "FlowDelta order service",
            "type": "httpHeaderAuth",
            "data": {"name": "x-flowdelta-service", "value": tokens["service"]},
        },
    ]
    (private / "import/credentials.json").write_bytes(json_bytes(credentials))

    record: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "variant": variant,
        "input": input_name,
        "workflow_sha256": digest((HERE / "workflows" / f"{variant}.json").read_bytes()),
        "initial_state_file_sha256": digest(INITIAL.read_bytes()),
        "started_at": now(),
        "execution_status": "NOT_RUN",
        "error_code": None,
    }
    artifacts: dict[str, Any] = {}
    request_sent = False
    try:
        run_n8n = [*compose, "run", "--rm", "--no-deps", "n8n"]
        call([*run_n8n, "import:credentials", "--input=/import/credentials.json"], "credentials")
        call([*run_n8n, "import:workflow", "--input=/import/workflow.json"], "workflow")
        call([*run_n8n, "publish:workflow", f"--id={WORKFLOW_ID}"], "publish")
        call([*compose, "up", "--detach", "--no-build"], "up")
        wait("n8n-health")
        wait("fixture-health")
        container = call([*compose, "ps", "--quiet", "n8n"], "n8n-container", 60)
        network = call(
            [
                "docker",
                "network",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            "network",
            60,
        )
        internal = json.loads(call(["docker", "network", "inspect", network], "inspect", 60))[0]
        if not internal.get("Internal"):
            raise RunError("PRIVATE_NETWORK_REQUIRED")
        record["runtime"] = {
            "engine_version": call([*compose, "exec", "-T", "n8n", "n8n", "--version"], "ver", 60),
            "n8n_image_id": call(
                ["docker", "inspect", "--format", "{{.Image}}", container], "image", 60
            ),
            "fixture_image": FIXTURE_IMAGE,
            "fixture_ext_sha256": call(
                [
                    *compose,
                    "exec",
                    "-T",
                    "orders-api",
                    "/app/.venv/bin/python",
                    "-c",
                    "import hashlib;print(hashlib.sha256(open("
                    "'/app/experiment/fixture_ext.py','rb').read()).hexdigest())",
                ],
                "fixture-hash",
                60,
            ),
            "network_internal": True,
        }
        initial = state()
        if initial.get("events") or initial.get("run_id") != run_id:
            raise RunError("INITIAL_STATE_MISMATCH")
        artifacts["initial-state.json"] = artifact(initial)
        request = {
            "method": "POST",
            "path": WEBHOOK,
            "body": body,
            "authentication": "local-credential-reference",
        }
        artifacts["request.json"] = artifact(request)
        request_sent = True
        status, answer = probe("normal-request", {"x-flowdelta-client": tokens["client"]}, body)
        artifacts["response.json"] = artifact({"status_code": status, "body": answer})
        final = state()
        artifacts["final-state.json"] = artifact(final)
        artifacts["events.json"] = artifact(
            {
                "schema_version": "0.4.0",
                "run_id": run_id,
                "events": final.get("events", []),
                "collection_status": "COMPLETE",
            }
        )
        record["execution_status"] = "SUCCEEDED"
    except (RunError, subprocess.SubprocessError, ValueError) as exc:
        record["execution_status"] = "FAILED" if request_sent else "NOT_RUN"
        record["error_code"] = str(exc) or type(exc).__name__
    finally:
        try:
            call([*compose, "logs", "--no-color"], "logs", 60)
        except RunError:
            pass
        try:
            call([*compose, "down", "--volumes", "--remove-orphans"], "down")
            left = call(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                "left",
                60,
            )
            record["cleanup_status"] = "COMPLETE" if not left else "FAILED"
        except RunError:
            record["cleanup_status"] = "FAILED"
        (private / "run.log").write_text("\n".join(log), encoding="utf-8", newline="\n")
    record["finished_at"] = now()
    record["embedded_artifacts"] = artifacts
    return record


def main() -> None:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    only = set(sys.argv[1:])
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for variant in expected["expected"]:
        for input_name, body in expected["inputs"].items():
            label = f"{variant}__{input_name}"
            if only and label not in only and variant not in only:
                continue
            record = run_one(variant, input_name, body)
            record["runtime_lock_engine_version"] = lock["engine_version"]
            out = EVIDENCE / f"{label}.json"
            out.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            response = record["embedded_artifacts"].get("response.json", {}).get("content", {})
            print(
                label,
                record["execution_status"],
                record["error_code"],
                response.get("status_code"),
                json.dumps(response.get("body"))[:80],
                flush=True,
            )


if __name__ == "__main__":
    main()
