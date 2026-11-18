"""Real HTTP contract checks for the opt-in synthetic fixture, not a C/judge evaluation."""

import argparse
import json
import secrets
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowdelta.baseline.fixture import NORMAL_PROFILE, OWNERSHIP_PROFILE
from flowdelta.ingestion import digest, json_bytes
from flowdelta.pipeline_contracts import EvidenceEvent
from flowdelta.runtime.order_lookup import PROBE, subprocess_runner

PROFILE_PATH = "runtime/order-ownership/fixture-profile.json"


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def check_profile(repo: Path) -> dict[str, Any]:
    profile: dict[str, Any] = json.loads((repo / PROFILE_PATH).read_bytes())
    require(profile["profile_id"] == OWNERSHIP_PROFILE, "UNKNOWN_PROFILE")
    for name, expected in profile["files"].items():
        path = (repo / name).resolve()
        require(path.is_relative_to(repo.resolve()), "PROFILE_PATH_OUTSIDE_REPOSITORY")
        require(digest(path.read_bytes()) == expected, "REGISTERED_FIXTURE_INPUT_CHANGED")
    return profile


def _run(repo: Path, out: Path, profile: dict[str, Any], *, evaluation: bool) -> dict[str, Any]:
    run_id, request_id = f"run-{uuid4().hex}", f"request-{uuid4().hex}"
    project = f"fdow-{uuid4().hex[:16]}"
    private = out / "private"
    private.mkdir(parents=True, mode=0o700)
    for name in ("fixture", "import", "n8n"):
        (private / name).mkdir(mode=0o777)
        (private / name).chmod(0o777)
    shutil.copyfile(repo / profile["initial_state"], private / "import/initial.json")
    service, control = secrets.token_hex(32), secrets.token_hex(32)
    env_file = private / ".env"
    env_file.write_text(
        f'FD_RUN_DIR="{private.as_posix()}"\nFD_RUN_ID={run_id}\n'
        f"FD_FIXTURE_IMAGE={profile['fixture_image']}\nFD_REQUEST_ID={request_id}\n"
        f"FD_ENCRYPTION_KEY={secrets.token_hex(32)}\n"
        f"FD_SERVICE_TOKEN={service}\nFD_CONTROL_TOKEN={control}\n",
        encoding="utf-8",
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
        str(repo / "runtime/order-lookup/compose.yaml"),
    ]
    if evaluation:
        compose.extend(["--file", str(repo / "runtime/order-ownership/compose.override.yaml")])
    report: dict[str, Any] = {
        "run_id": run_id,
        "fixture_profile": OWNERSHIP_PROFILE if evaluation else NORMAL_PROFILE,
        "request_id": request_id if evaluation else None,
        "checks": [],
        "status": "FAILED",
        "cleanup": "NOT_ATTEMPTED",
        "security_outcome": None,
    }

    def command(args: list[str], payload: dict[str, Any] | None = None) -> str:
        result = subprocess_runner(
            args,
            timeout=180 if payload is None else 45,
            input_text=json.dumps(payload) if payload is not None else None,
        )
        # Raw Docker errors can contain credentials; only stable codes enter the report.
        require(result.returncode == 0, "DOCKER_COMMAND_FAILED")
        return result.stdout.strip()

    def probe(operation: str, **values: Any) -> dict[str, Any]:
        response: dict[str, Any] = json.loads(
            command(
                [*compose, "exec", "-T", "orders-api", *PROBE],
                {"operation": operation, "timeout_seconds": 5, **values},
            )
        )
        return response

    def check(
        operation: str,
        status: int,
        order_id: str = "ORD-2001",
        *,
        token: str | None = service,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = probe(
            operation,
            order_id=order_id,
            headers={**({"X-FlowDelta-Service": token} if token else {}), **(extra_headers or {})},
        )
        report["checks"].append(
            {
                "operation": operation,
                "order_id": order_id,
                "expected_status": status,
                "response": response,
            }
        )
        require(response.get("status_code") == status, "UNEXPECTED_HTTP_STATUS")
        body: dict[str, Any] = response["body"]
        return body

    try:
        image = json.loads(command(["docker", "image", "inspect", profile["fixture_image"]]))[0]
        report["fixture_image_id"] = image["Id"]
        command([*compose, "up", "-d", "--no-build", "--pull", "never", "orders-api"])
        for _ in range(30):
            if probe("fixture-health").get("status_code") == 200:
                break
            time.sleep(1)
        else:
            raise RuntimeError("FIXTURE_NOT_READY")
        metadata = probe("fixture-metadata")["body"]
        require(
            metadata["source_sha256"]
            == {
                name: profile["files"][f"src/flowdelta/baseline/{name}"]
                for name in ("fixture.py", "probe.py")
            },
            "FIXTURE_REBUILD_REQUIRED",
        )
        report["fixture_metadata"] = metadata
        lock = tomllib.loads((repo / "runtime/order-lookup/runtime.lock.toml").read_text("utf-8"))
        for name in ("python", "fastapi", "uvicorn"):
            require(metadata[name] == lock[f"{name}_version"], "FIXTURE_VERSION_CHANGED")
        container_id = command([*compose, "ps", "-q", "orders-api"])
        container = json.loads(command(["docker", "inspect", container_id]))[0]
        require(container["Image"] == image["Id"], "FIXTURE_IMAGE_CHANGED")
        require(not container["HostConfig"].get("PortBindings"), "HOST_PORT_PUBLISHED")
        networks = list(container["NetworkSettings"]["Networks"])
        require(len(networks) == 1, "UNEXPECTED_NETWORK")
        network = json.loads(command(["docker", "network", "inspect", networks[0]]))[0]
        require(network["Internal"] is True, "NETWORK_NOT_INTERNAL")
        report["isolation"] = {"internal_network": True, "host_ports": False}
        initial = probe("state", headers={"X-FlowDelta-Control": control})["body"]
        report["initial_state"] = initial
        require(initial["run_id"] == run_id and initial["events"] == [], "STATE_NOT_FRESH")
        require(
            initial["metadata"]["fixture_profile"] == report["fixture_profile"]
            and initial["metadata"]["request_id"] == (request_id if evaluation else ""),
            "FIXTURE_CONTEXT_CHANGED",
        )
        require(
            initial["metadata"]["initial_sha256"] == profile["files"][profile["initial_state"]],
            "INITIAL_STATE_CHANGED",
        )
        check("scoped-read", 200, "ORD-1001")
        check("scoped-read", 404)
        for operation in ("ownership-context", "ownership-metadata", "ownership-read"):
            check(operation, 401 if evaluation else 404, token=None)
            check(operation, 401 if evaluation else 404, token=control)
            if not evaluation:
                check(operation, 404)
        if evaluation:
            context = check(
                "ownership-context",
                200,
                extra_headers={
                    "X-FlowDelta-Principal": "other-demo",
                    "X-FlowDelta-Request-Id": "forged",
                },
            )
            require(
                context
                == {
                    "principal": "customer-demo",
                    "tenant": "tenant-demo",
                    "request_id": request_id,
                    "run_id": run_id,
                },
                "REQUEST_CONTEXT_SPOOFED",
            )
            owner = check("ownership-metadata", 200)
            require(set(owner) == {"order_id", "customer_id", "tenant_id"}, "METADATA_LEAK")
            # Rehearse a pre-read guard; this is not a workflow evaluation or a sealed label.
            require(
                (owner["customer_id"], owner["tenant_id"])
                != (context["principal"], context["tenant"]),
                "FOREIGN_ORDER_NOT_FOREIGN",
            )
            check("ownership-read", 200)
            own = check("ownership-metadata", 200, "ORD-1001")
            require(
                (own["customer_id"], own["tenant_id"]) == (context["principal"], context["tenant"]),
                "OWN_ORDER_NOT_OWN",
            )
            check("ownership-read", 200, "ORD-1001")
            check("ownership-read", 404, "ORD-9999")
            check("ownership-metadata", 404, "ORD-9999")
        final = probe("state", headers={"X-FlowDelta-Control": control})["body"]
        report["final_state"] = final
        require(final["orders_sha256"] == initial["orders_sha256"], "ORDER_STATE_CHANGED")
        events = [EvidenceEvent.model_validate(event) for event in final["events"]]
        require(
            [event.sequence for event in events] == list(range(1, len(events) + 1)),
            "EVENT_SEQUENCE_INVALID",
        )
        require(len(events) == (8 if evaluation else 2), "EVENT_COUNT_INVALID")
        for event in events:
            require(
                event.run_id == run_id
                and event.principal == "customer-demo"
                and event.tenant == "tenant-demo",
                "EVENT_CONTEXT_INVALID",
            )
            if evaluation:
                require(event.attributes["request_id"] == request_id, "EVENT_REQUEST_MISMATCH")
        foreign_reads = [
            event
            for event in events
            if event.resource_id == "ORD-2001"
            and event.event_type == "ORDER_READ"
            and event.outcome == "RETURNED"
        ]
        require(len(foreign_reads) == (1 if evaluation else 0), "READ_EFFECT_NOT_OBSERVED")
        if evaluation:
            require(
                foreign_reads[0].attributes["resource_customer_id"] == "other-demo"
                and foreign_reads[0].attributes["resource_tenant_id"] == "other-tenant",
                "RESOURCE_OWNERSHIP_MISSING",
            )
        report["status"] = "PASSED"
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        report["failure"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        try:
            command([*compose, "down", "--volumes", "--remove-orphans", "--timeout", "10"])
            selector = f"label=com.docker.compose.project={project}"
            remaining = [
                command(["docker", "ps", "-aq", "--filter", selector]),
                command(["docker", "network", "ls", "-q", "--filter", selector]),
                command(["docker", "volume", "ls", "-q", "--filter", selector]),
            ]
            require(not any(remaining), "CLEANUP_INCOMPLETE")
            report["cleanup"] = "SUCCEEDED"
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            report["cleanup"] = "FAILED"
            report["status"] = "FAILED"
        (out / "report.json").write_bytes(json_bytes(report))
    return report


def run_checks(repo: Path, out: Path) -> dict[str, Any]:
    repo, out = repo.resolve(), out.resolve()
    require(out.is_relative_to(repo / "var") and out != repo / "var", "OUTPUT_OUTSIDE_VAR")
    require(not out.exists(), "OUTPUT_ALREADY_EXISTS")
    profile = check_profile(repo)
    out.mkdir(parents=True)
    reports = [
        _run(repo, out / name, profile, evaluation=evaluation)
        for name, evaluation in (("normal", False), ("evaluation", True), ("fresh", True))
    ]
    summary = {
        "profile_id": OWNERSHIP_PROFILE,
        "kind": "FIXTURE_CONTRACT_CHECK",
        "status": "PASSED" if all(r["status"] == "PASSED" for r in reports) else "FAILED",
        "profile_sha256": digest((repo / PROFILE_PATH).read_bytes()),
        "security_outcome": None,
        "runs": [{key: r[key] for key in ("run_id", "status", "cleanup")} for r in reports],
    }
    (out / "summary.json").write_bytes(json_bytes(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    summary = run_checks(Path.cwd(), args.out)
    print(json_bytes(summary).decode())
    raise SystemExit(0 if summary["status"] == "PASSED" else 1)
