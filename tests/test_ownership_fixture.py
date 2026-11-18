import asyncio
import json
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import EXAMPLE
from fastapi import HTTPException

from flowdelta.baseline import ownership as ownership_check
from flowdelta.baseline.fixture import (
    NORMAL_PROFILE,
    OWNERSHIP_PROFILE,
    OrderStore,
    app,
    lifespan,
)
from flowdelta.baseline.ownership import check_profile, run_checks
from flowdelta.ingestion import digest, json_bytes
from flowdelta.pipeline_contracts import EvidenceEvent
from flowdelta.runtime.order_lookup import CommandResult

INITIAL = EXAMPLE / "state/initial.json"
SERVICE = "synthetic-service-credential"
CONTROL = "synthetic-control-credential"


def http_get(path, *, headers=None, body=None):
    """Exercise ASGI routing, validation and authentication without another HTTP dependency."""
    response = []
    target = urlsplit(path)

    async def receive():
        return {"type": "http.request", "body": json_bytes(body) if body else b""}

    async def send(message):
        response.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": target.path,
        "root_path": "",
        "query_string": target.query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "server": ("fixture", 8080),
        "client": ("workflow", 1234),
    }
    asyncio.run(app(scope, receive, send))
    return response[0]["status"], json.loads(b"".join(m.get("body", b"") for m in response[1:]))


@pytest.fixture
def bind_store(tmp_path, monkeypatch):
    def bind(*, evaluation=True):
        store = OrderStore(
            tmp_path,
            INITIAL,
            "run-test",
            profile=OWNERSHIP_PROFILE if evaluation else NORMAL_PROFILE,
            request_id="request-test" if evaluation else "",
        )
        monkeypatch.setattr(app.state, "store", store, raising=False)
        monkeypatch.setattr(app.state, "service_token", SERVICE, raising=False)
        monkeypatch.setattr(app.state, "control_token", CONTROL, raising=False)
        return store

    return bind


@pytest.mark.parametrize(
    "path",
    [
        "/internal/request-context",
        "/internal/orders/ORD-2001",
        "/internal/orders/ORD-2001/ownership",
    ],
)
def test_internal_routes_are_off_in_normal_profile(bind_store, path):
    store = bind_store(evaluation=False)
    assert http_get(path, headers={"X-FlowDelta-Service": SERVICE})[0] == 404
    assert store.state()["events"] == []


@pytest.mark.parametrize(
    "path",
    [
        "/internal/request-context",
        "/internal/orders/ORD-2001",
        "/internal/orders/ORD-2001/ownership",
    ],
)
@pytest.mark.parametrize("token", [None, "wrong", CONTROL])
def test_internal_routes_require_service_credential(bind_store, path, token):
    store = bind_store()
    assert http_get(path, headers={"X-FlowDelta-Service": token} if token else {})[0] == 401
    assert store.state()["events"] == []


def test_read_effects_and_metadata_are_independent_and_request_bound(bind_store):
    store = bind_store()
    initial = store.state()
    headers = {"X-FlowDelta-Service": SERVICE}
    _, context = http_get(
        "/internal/request-context",
        headers={
            **headers,
            "X-FlowDelta-Principal": "other-demo",
            "X-FlowDelta-Request-Id": "forged",
        },
        body={"principal": "other-demo", "tenant": "other-tenant", "request_id": "forged"},
    )
    assert context == {
        "principal": "customer-demo",
        "tenant": "tenant-demo",
        "request_id": "request-test",
        "run_id": "run-test",
    }
    status, owner = http_get("/internal/orders/ORD-2001/ownership", headers=headers)
    assert status == 200
    assert owner == {
        "order_id": "ORD-2001",
        "customer_id": "other-demo",
        "tenant_id": "other-tenant",
    }
    # A guard can refuse here, before the protected data is read.
    metadata_event = EvidenceEvent.model_validate(store.state()["events"][0])
    assert metadata_event.event_type == "ORDER_METADATA_READ"
    assert metadata_event.response_sha256 == digest(json_bytes(owner))
    status, protected = http_get("/internal/orders/ORD-2001", headers=headers)
    assert status == 200 and protected == {"order_id": "ORD-2001", "status": "배송중"}
    event = EvidenceEvent.model_validate(store.state()["events"][1])
    assert event.event_type == "ORDER_READ" and event.outcome == "RETURNED"
    assert event.principal == "customer-demo" and event.tenant == "tenant-demo"
    assert event.response_sha256 == digest(json_bytes(protected))
    assert event.attributes == {
        "fixture_profile": OWNERSHIP_PROFILE,
        "request_id": "request-test",
        "access_path": "service",
        "resource_customer_id": "other-demo",
        "resource_tenant_id": "other-tenant",
    }
    assert store.state()["orders_sha256"] == initial["orders_sha256"]
    assert http_get("/__fixture__/state", headers=headers)[0] == 401
    assert http_get("/__fixture__/state", headers={"X-FlowDelta-Control": CONTROL})[0] == 200


@pytest.mark.parametrize(
    "path",
    [
        "/orders/lookup?order_id=ORD-2001",
        "/internal/orders/ORD-9999",
        "/internal/orders/ORD-9999/ownership",
    ],
)
def test_denial_or_missing_record_is_not_a_returned_read(bind_store, path):
    store = bind_store()
    status, body = http_get(path, headers={"X-FlowDelta-Service": SERVICE})
    assert status == 404
    event = EvidenceEvent.model_validate(store.state()["events"][0])
    assert event.outcome == "NOT_FOUND"
    assert event.response_sha256 == digest(json_bytes(body))


@pytest.mark.parametrize("order_id", ["invalid", "ORD-10010", "ORD-000%0A", "ORD-1001%3Fother=1"])
def test_internal_path_validates_order_id_before_recording_read(bind_store, order_id):
    store = bind_store()
    assert (
        http_get(f"/internal/orders/{order_id}", headers={"X-FlowDelta-Service": SERVICE})[0] == 422
    )
    assert store.state()["events"] == []


@pytest.mark.parametrize("query", ["owner=customer-demo", "tenant=tenant-demo", "owner="])
def test_experiment_query_filters_cannot_be_silently_ignored(bind_store, query):
    store = bind_store()
    status, body = http_get(
        f"/internal/orders/ORD-2001?{query}", headers={"X-FlowDelta-Service": SERVICE}
    )
    assert status == 422 and body == {"detail": "query_filters_not_supported"}
    assert store.state()["events"] == []


@pytest.mark.parametrize(
    "customer,tenant", [("other-demo", "tenant-demo"), ("customer-demo", "other-tenant")]
)
def test_scoped_path_checks_both_customer_and_tenant(tmp_path, customer, tenant):
    source = json.loads(INITIAL.read_bytes())
    source["data"]["orders"][0].update(customer_id=customer, tenant_id=tenant)
    initial = tmp_path / "initial.json"
    initial.write_bytes(json_bytes(source))
    store = OrderStore(
        tmp_path / "state",
        initial,
        "run-test",
        profile=OWNERSHIP_PROFILE,
        request_id="request-test",
    )
    with pytest.raises(HTTPException) as error:
        store.lookup("ORD-1001")
    assert error.value.status_code == 404
    assert store.internal_lookup("ORD-1001")["order_id"] == "ORD-1001"


@pytest.mark.parametrize(
    "profile,request_id,error",
    [
        ("unknown", "", "UNKNOWN_FIXTURE_PROFILE"),
        (OWNERSHIP_PROFILE, "", "EVALUATION_REQUEST_ID_REQUIRED"),
        (OWNERSHIP_PROFILE, "request\nforged", "EVALUATION_REQUEST_ID_REQUIRED"),
        (NORMAL_PROFILE, "request-test", "REQUEST_ID_REQUIRES_EVALUATION_PROFILE"),
    ],
)
def test_fixture_rejects_invalid_context(tmp_path, profile, request_id, error):
    with pytest.raises(RuntimeError, match=error):
        OrderStore(tmp_path, INITIAL, "run-test", profile=profile, request_id=request_id)
    assert not (tmp_path / "orders.sqlite3").exists()


def test_cannot_rebind_existing_state_to_new_profile_or_request(tmp_path):
    OrderStore(tmp_path / "normal", INITIAL, "run-test")
    with pytest.raises(RuntimeError, match="FIXTURE_CONTEXT_CHANGED"):
        OrderStore(
            tmp_path / "normal",
            INITIAL,
            "run-test",
            profile=OWNERSHIP_PROFILE,
            request_id="request-1",
        )
    first = OrderStore(
        tmp_path / "eval", INITIAL, "run-test", profile=OWNERSHIP_PROFILE, request_id="request-1"
    )
    first.internal_lookup("ORD-2001")
    for profile, request_id in [(NORMAL_PROFILE, ""), (OWNERSHIP_PROFILE, "request-2")]:
        with pytest.raises(RuntimeError, match="FIXTURE_CONTEXT_CHANGED"):
            OrderStore(
                tmp_path / "eval", INITIAL, "run-test", profile=profile, request_id=request_id
            )
    reopened = OrderStore(
        tmp_path / "eval", INITIAL, "run-test", profile=OWNERSHIP_PROFILE, request_id="request-1"
    )
    assert len(reopened.state()["events"]) == 1


def test_lifespan_binds_explicit_evaluation_environment(tmp_path, monkeypatch):
    for key, value in {
        "FD_STATE_DIR": str(tmp_path),
        "FD_INITIAL_STATE": str(INITIAL),
        "FD_RUN_ID": "run-env",
        "FD_SERVICE_TOKEN": SERVICE,
        "FD_CONTROL_TOKEN": CONTROL,
        "FD_FIXTURE_PROFILE": OWNERSHIP_PROFILE,
        "FD_REQUEST_ID": "request-env",
    }.items():
        monkeypatch.setenv(key, value)

    async def start():
        async with lifespan(app):
            assert app.state.store.request_context()["request_id"] == "request-env"

    asyncio.run(start())


@pytest.mark.parametrize(
    "registered_path",
    [None, "src/flowdelta/runtime/order_lookup.py"],
    ids=["initial-state", "runtime-source"],
)
def test_registered_profile_and_input_tampering(tmp_path, registered_path):
    repo = Path(__file__).resolve().parents[1]
    profile = check_profile(repo)
    for name in ["runtime/order-ownership/fixture-profile.json", *profile["files"]]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repo / name).read_bytes())
    changed_input = registered_path or profile["initial_state"]
    assert changed_input in profile["files"]
    target = tmp_path / changed_input
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(RuntimeError, match="REGISTERED_FIXTURE_INPUT_CHANGED"):
        check_profile(tmp_path)


def test_contract_check_rejects_output_outside_var_before_docker(tmp_path):
    with pytest.raises(RuntimeError, match="OUTPUT_OUTSIDE_VAR"):
        run_checks(tmp_path, tmp_path / "evidence")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_stale_image_stops_before_business_request_and_always_attempts_cleanup(
    tmp_path, monkeypatch, cleanup_fails
):
    repo = Path(__file__).resolve().parents[1]
    operations, commands = [], []

    def fake_runner(args, *, timeout, input_text=None):
        commands.append(args)
        if input_text:
            operation = json.loads(input_text)["operation"]
            operations.append(operation)
            body = {"source_sha256": {}} if operation == "fixture-metadata" else {"status": "ready"}
            return CommandResult(0, json.dumps({"status_code": 200, "body": body}), "")
        if args[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, json.dumps([{"Id": "synthetic-image-id"}]), "")
        if "down" in args and cleanup_fails:
            return CommandResult(1, "", "sensitive diagnostic must not enter report")
        return CommandResult(0, "", "")

    monkeypatch.setattr(ownership_check, "subprocess_runner", fake_runner)
    report = ownership_check._run(repo, tmp_path / "run", check_profile(repo), evaluation=True)
    assert report["status"] == "FAILED"
    assert report["failure"] == "FIXTURE_REBUILD_REQUIRED"
    assert report["cleanup"] == ("FAILED" if cleanup_fails else "SUCCEEDED")
    assert operations == ["fixture-health", "fixture-metadata"]
    assert any("down" in command for command in commands)
    assert "sensitive diagnostic" not in (tmp_path / "run/report.json").read_text("utf-8")


@pytest.fixture
def ownership_docker(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    profile = check_profile(source)
    repo = tmp_path / "repo"
    for name in [ownership_check.PROFILE_PATH, *profile["files"]]:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    versions = tomllib.loads((repo / "runtime/order-lookup/runtime.lock.toml").read_text("utf-8"))
    failures, queries, projects = {}, [], []
    project = None

    def fake_runner(args, *, timeout, input_text=None):
        nonlocal project
        if input_text is not None:
            request = json.loads(input_text)
            operation = request["operation"]
            if operation == "fixture-health":
                status, body = 200, {"status": "ready"}
            elif operation == "fixture-metadata":
                status = 200
                body = {
                    "source_sha256": {
                        name: profile["files"][f"src/flowdelta/baseline/{name}"]
                        for name in ("fixture.py", "probe.py")
                    },
                    **{
                        name: versions[f"{name}_version"]
                        for name in ("python", "fastapi", "uvicorn")
                    },
                }
            else:
                order_id = request.get("order_id", "ORD-2001")
                path = {
                    "state": "/__fixture__/state",
                    "scoped-read": f"/orders/lookup?order_id={order_id}",
                    "ownership-context": "/internal/request-context",
                    "ownership-metadata": f"/internal/orders/{order_id}/ownership",
                    "ownership-read": f"/internal/orders/{order_id}",
                }[operation]
                status, body = http_get(path, headers=request.get("headers"))
            return CommandResult(0, json.dumps({"status_code": status, "body": body}), "")
        if args[:2] == ["docker", "compose"]:
            if "up" in args:
                project = args[args.index("--project-name") + 1]
                projects.append(project)
                env_file = Path(args[args.index("--env-file") + 1])
                environment = dict(
                    line.split("=", 1) for line in env_file.read_text("utf-8").splitlines()
                )
                evaluation = any(arg.endswith("compose.override.yaml") for arg in args)
                store = OrderStore(
                    env_file.parent / "fixture",
                    repo / profile["initial_state"],
                    environment["FD_RUN_ID"],
                    profile=OWNERSHIP_PROFILE if evaluation else NORMAL_PROFILE,
                    request_id=environment["FD_REQUEST_ID"] if evaluation else "",
                )
                monkeypatch.setattr(app.state, "store", store, raising=False)
                monkeypatch.setattr(
                    app.state, "service_token", environment["FD_SERVICE_TOKEN"], raising=False
                )
                monkeypatch.setattr(
                    app.state, "control_token", environment["FD_CONTROL_TOKEN"], raising=False
                )
                return CommandResult(0, "", "")
            if "ps" in args:
                return CommandResult(0, "synthetic-container", "")
            if "down" in args:
                return CommandResult(0, "", "")
        if args[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, json.dumps([{"Id": "synthetic-image"}]), "")
        if args[:2] == ["docker", "inspect"]:
            container = {
                "Image": "synthetic-image",
                "HostConfig": {"PortBindings": {}},
                "NetworkSettings": {"Networks": {"synthetic-network": {}}},
            }
            return CommandResult(0, json.dumps([container]), "")
        if args[:3] == ["docker", "network", "inspect"]:
            return CommandResult(0, json.dumps([{"Internal": True}]), "")
        resource = {
            ("docker", "ps", "-aq"): "container",
            ("docker", "network", "ls"): "network",
            ("docker", "volume", "ls"): "volume",
        }.get(tuple(args[:3]))
        if resource:
            assert args[args.index("--filter") + 1] == f"label=com.docker.compose.project={project}"
            queries.append((project, resource))
            failure = failures.get(resource)
            if failure == "leftover":
                return CommandResult(0, "remaining-resource\n", "")
            if failure == "query-error":
                return CommandResult(1, "", "sensitive diagnostic must not enter report")
            if failure == "query-timeout":
                raise subprocess.TimeoutExpired(args, timeout, stderr="sensitive diagnostic")
            if failure == "query-os-error":
                raise OSError("sensitive diagnostic")
            return CommandResult(0, " \n", "")
        raise AssertionError(f"Unexpected Docker command: {args[:3]}")

    monkeypatch.setattr(ownership_check, "subprocess_runner", fake_runner)
    return repo, failures, queries, projects


def test_ownership_cleanup_succeeds_only_after_all_project_resources_are_absent(ownership_docker):
    repo, _, queries, projects = ownership_docker
    summary = run_checks(repo, repo / "var/checks")
    assert summary["status"] == "PASSED"
    assert {run["cleanup"] for run in summary["runs"]} == {"SUCCEEDED"}
    assert len(set(projects)) == 3
    assert set(queries) == {
        (project, resource)
        for project in projects
        for resource in ("container", "network", "volume")
    }


@pytest.mark.parametrize("resource", ["container", "network", "volume"])
@pytest.mark.parametrize("failure", ["leftover", "query-error", "query-timeout", "query-os-error"])
def test_ownership_cleanup_failure_fails_completed_checks(ownership_docker, resource, failure):
    repo, failures, queries, projects = ownership_docker
    failures[resource] = failure
    output = repo / "var/checks"
    summary = run_checks(repo, output)
    assert summary["status"] == "FAILED"
    assert {run["status"] for run in summary["runs"]} == {"FAILED"}
    assert {run["cleanup"] for run in summary["runs"]} == {"FAILED"}
    assert {(project, resource) for project in projects}.issubset(set(queries))
    assert json.loads((output / "summary.json").read_bytes()) == summary
    for name in ("normal", "evaluation", "fresh"):
        report_text = (output / name / "report.json").read_text("utf-8")
        report = json.loads(report_text)
        assert report["final_state"]  # Business checks completed before the cleanup failure.
        assert "failure" not in report
        assert report["status"] == "FAILED" and report["cleanup"] == "FAILED"
        assert "sensitive diagnostic" not in report_text
