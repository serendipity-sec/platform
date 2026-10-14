"""A scripted stand-in for the Docker CLI, so runtime tests need no containers.

It answers the commands `flowdelta.runtime.order_lookup` sends, keeps one fake order service per
Compose project, and can fail chosen steps (timeout, non-zero exit, missing image).
"""

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from flowdelta.ingestion import digest, json_bytes
from flowdelta.runtime.order_lookup import CommandResult

REPO = Path(__file__).resolve().parents[2]
LOCK = tomllib.loads((REPO / "runtime/order-lookup/runtime.lock.toml").read_text("utf-8"))
IMAGE_ID = "sha256:" + "a" * 64


@dataclass
class Project:
    run_id: str
    initial_sha256: str
    secrets: list[str]
    up: bool = False
    events: list[dict[str, object]] = field(default_factory=list)


@dataclass
class FakeDocker:
    # step name -> "timeout" | "fail"; steps: image, import-credentials, start-services,
    # normal-request, final-state, down, remaining
    failures: dict[str, str] = field(default_factory=dict)
    response_status: int = 200
    foreign_event: bool = False
    calls: list[list[str]] = field(default_factory=list)
    projects: dict[str, Project] = field(default_factory=dict)
    requests: list[tuple[object, float]] = field(default_factory=list)

    def __call__(
        self, args: list[str], *, timeout: float, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(args)
        if args[:2] == ["docker", "compose"]:
            return self.compose(args, timeout, input_text)
        if args[:3] == ["docker", "image", "inspect"]:
            return self.step("image", IMAGE_ID)
        if args[:2] == ["docker", "inspect"]:
            if "{{.Image}}" in args:
                return CommandResult(0, IMAGE_ID, "")
            return CommandResult(0, "{}", "")  # no published ports
        if args[:3] == ["docker", "network", "inspect"]:
            return CommandResult(0, json.dumps([{"Internal": True}]), "")
        if "--filter" in args:  # ps/network/volume listing for one project label
            name = args[args.index("--filter") + 1].split("=", 2)[-1]
            project = self.projects[name]
            if args[:3] == ["docker", "network", "ls"] and project.up:
                return CommandResult(0, "network-1", "")
            return self.step("remaining", "")
        raise AssertionError(f"unexpected command {args}")

    def step(self, name: str, stdout: str) -> CommandResult:
        mode = self.failures.get(name)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(name, 1)
        if mode == "fail":
            return CommandResult(1, "", f"{name} failed")
        return CommandResult(0, stdout, "")

    def compose(self, args: list[str], timeout: float, input_text: str | None) -> CommandResult:
        name = args[args.index("--project-name") + 1]
        if name not in self.projects:
            env = Path(args[args.index("--env-file") + 1]).read_text("utf-8")
            values = dict(line.split("=", 1) for line in env.splitlines())
            initial = Path(values["FD_RUN_DIR"].strip('"')) / "import/initial.json"
            secrets = [values["FD_ENCRYPTION_KEY"], values["FD_SERVICE_TOKEN"]]
            secrets.append(values["FD_CONTROL_TOKEN"])
            credentials = Path(values["FD_RUN_DIR"].strip('"')) / "import/credentials.json"
            if credentials.exists():
                secrets += [item["data"]["value"] for item in json.loads(credentials.read_bytes())]
            self.projects[name] = Project(
                values["FD_RUN_ID"],
                digest(initial.read_bytes()) if initial.exists() else "",
                secrets,
            )
        project = self.projects[name]
        command = args[args.index("--file") + 2 :]
        if command[:3] == ["run", "--rm", "--no-deps"]:
            step = {"import:credentials": "import-credentials"}.get(command[4], "import")
            return self.step(step, "imported")
        if command[0] == "up":
            project.up = True
            return self.step("start-services", "started")
        if command[0] == "down":
            result = self.step("down", "removed")
            project.up = project.up and result.returncode != 0
            return result
        if command[0] == "logs":
            # A real log could echo a secret; the runner must redact it before saving.
            return CommandResult(0, f"n8n ready {project.secrets[0]}", "")
        if command[0] == "ps":
            return CommandResult(0, "container-1", "")
        if command[:4] == ["exec", "-T", "n8n", "n8n"]:
            return CommandResult(0, LOCK["engine_version"], "")
        if command[:3] == ["exec", "-T", "orders-api"]:
            return self.probe(project, json.loads(input_text or "{}"), timeout)
        raise AssertionError(f"unexpected compose command {command}")

    def probe(self, project: Project, payload: dict[str, object], timeout: float) -> CommandResult:
        operation = payload["operation"]
        body: object
        if operation in {"n8n-health", "fixture-health"}:
            body = {"status": "ready"}
        elif operation == "fixture-metadata":
            body = {
                "source_sha256": {
                    name: digest((REPO / "src/flowdelta/baseline" / name).read_bytes())
                    for name in ["fixture.py", "probe.py"]
                },
                "python": LOCK["python_version"],
                "fastapi": LOCK["fastapi_version"],
                "uvicorn": LOCK["uvicorn_version"],
            }
        elif operation == "state":
            if project.events and self.failures.get("final-state") == "fail":
                return CommandResult(1, "", "state unavailable")
            orders = [{"order_id": "ORD-1001", "status": "준비중"}]
            body = {
                "schema_version": "0.4.0",
                "run_id": project.run_id,
                "metadata": {"run_id": project.run_id, "initial_sha256": project.initial_sha256},
                "orders": orders,
                "orders_sha256": digest(json_bytes(orders)),
                "events": project.events,
            }
        elif operation == "normal-request":
            self.requests.append((payload["body"], timeout))
            owner = "run-someone-else" if self.foreign_event else project.run_id
            project.events.append({"run_id": owner, "event_type": "ORDER_READ", "sequence": 1})
            result = self.step("normal-request", "")
            if result.returncode:
                return result
            body = {"order_id": "ORD-1001", "status": "준비중"}
            return CommandResult(
                0, json.dumps({"status_code": self.response_status, "body": body}), ""
            )
        else:
            raise AssertionError(f"unexpected probe {operation}")
        return CommandResult(0, json.dumps({"status_code": 200, "body": body}), "")
