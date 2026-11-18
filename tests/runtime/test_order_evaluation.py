"""A/B/C contract tests with a Docker double; real n8n validates behavior separately."""

import json
import shutil
from pathlib import Path

import pytest

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.module_outputs import read_module_outputs
from flowdelta.order_evaluation import (
    comparison_outcome,
    evaluate_order_family,
    judge_evaluation_bundle,
)
from flowdelta.order_scenarios import ROOT, SCENARIO_KEY, registered_files
from flowdelta.runtime import order_lookup
from flowdelta.runtime.order_lookup import CommandResult
from flowdelta.runtime.preflight import preflight, read_plan_bundle

from .fake_docker import FakeDocker

REPO = Path(__file__).resolve().parents[2]


class OrderDocker(FakeDocker):
    """Return independently shaped service events, not results inferred from A/B output."""

    def __init__(self, *, interrupt: str | None = None) -> None:
        super().__init__()
        self.contexts = {}
        self.interrupt = interrupt

    def compose(self, args, timeout, input_text):
        values = dict(
            line.split("=", 1)
            for line in Path(args[args.index("--env-file") + 1]).read_text("utf-8").splitlines()
        )
        initial = json.loads(
            (Path(values["FD_RUN_DIR"].strip('"')) / "import/initial.json").read_bytes()
        )
        self.contexts[values["FD_RUN_ID"]] = (
            values.get("FD_REQUEST_ID", ""),
            initial["data"]["orders"],
        )
        if self.interrupt == "logs" and "logs" in args:
            raise KeyboardInterrupt
        return super().compose(args, timeout, input_text)

    def probe(self, project, payload, timeout):
        operation = payload["operation"]
        request_id, orders = self.contexts[project.run_id]
        if operation == "normal-request":
            if self.interrupt == "request":
                raise KeyboardInterrupt
            self.requests.append((payload["body"], timeout))
            body_input = payload["body"]
            order = next(o for o in orders if o["order_id"] == body_input["order_id"])
            body = {k: order[k] for k in ("order_id", "status")}
            project.events.append(
                {
                    "schema_version": "0.4.0",
                    "run_id": project.run_id,
                    "producer": "orders-api",
                    "sequence": 1,
                    "observed_at": "2026-11-17T00:00:00Z",
                    "event_type": "ORDER_READ",
                    "principal": "customer-demo",
                    "tenant": "tenant-demo",
                    "resource_id": order["order_id"],
                    "outcome": "RETURNED",
                    "method": "GET",
                    "response_sha256": digest(json_bytes(body)),
                    "attributes": {
                        "fixture_profile": "order-ownership-eval-v1",
                        "request_id": request_id,
                        "resource_customer_id": order["customer_id"],
                        "resource_tenant_id": order["tenant_id"],
                        "access_path": "service",
                    },
                }
            )
        elif operation == "state":
            body = {
                "schema_version": "0.4.0",
                "run_id": project.run_id,
                "metadata": {
                    "run_id": project.run_id,
                    "initial_sha256": project.initial_sha256,
                    "fixture_profile": "order-ownership-eval-v1",
                    "request_id": request_id,
                },
                "orders": orders,
                "orders_sha256": digest(json_bytes(orders)),
                "events": project.events,
            }
        else:
            return super().probe(project, payload, timeout)
        return CommandResult(0, json.dumps({"status_code": 200, "body": body}), "")


@pytest.fixture(scope="module")
def evaluation(tmp_path_factory):
    repo = tmp_path_factory.mktemp("order-family") / "repo"
    for path in [f"{ROOT}/approved-inputs.json", *registered_files(REPO)]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / path, target)
    docker = OrderDocker()
    output = repo / "var/e"
    summary = evaluate_order_family(
        repo=repo,
        manifest=repo / ROOT / "family.json",
        output=output,
        execute=True,
        runner=docker,
        sleep=lambda _: None,
    )
    return repo, output, summary, docker


def test_b_plans_are_consumed_by_c_with_isolated_evidence(evaluation):
    repo, output, summary, docker = evaluation
    assert summary["status"] == "COMPLETE"
    assert len(summary["pairs"]) == 6
    assert len(docker.projects) == 36
    ids = []
    requests = []
    for pair in summary["pairs"]:
        assert len(pair["trials"]) == 3
        assert len({t["plan_id"] for t in pair["trials"]}) == 3
        for trial in pair["trials"]:
            bundle = output / trial["bundle"]
            assert read_module_outputs(bundle)["run_count"] == 2
            h, p, c = read_plan_bundle(bundle)
            assert p.normal_input == h.task.normal_input
            checked = preflight(h, p, c, bundle, repo)
            assert checked.request_input["order_id"] == (
                "ORD-1001" if trial["scenario_id"] == "normal" else "ORD-2001"
            )
            assert p.analysis_id == pair["analysis_id"]
            for record in trial["records"]:
                ids.append(record["run_id"])
                requests.append(record["request_id"])
    assert len(set(ids)) == len(set(requests)) == 36
    assert all(not p.up for p in docker.projects.values())


@pytest.mark.parametrize("mutation", ["policy", "pair", "normal-scenario", "forged-scenario"])
def test_unregistered_context_and_scenarios_are_rejected(evaluation, mutation):
    repo, output, _, _ = evaluation
    bundle = output / "p0/s1/p"
    h, p, c = read_plan_bundle(bundle)
    if mutation == "policy":
        h.source_manifest.artifacts[0].sha256 = "f" * 64
    elif mutation == "pair":
        h.case.family_id = "unregistered"
    elif mutation == "normal-scenario":
        p.template_id = "order-evaluation-normal"
    else:
        p.extensions[SCENARIO_KEY]["sha256"] = "f" * 64
    with pytest.raises(InputError):
        preflight(h, p, c, bundle, repo)


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("NOT_OBSERVED_COMPLETE", "VIOLATION_OBSERVED", "NEW_VIOLATION"),
        ("VIOLATION_OBSERVED", "VIOLATION_OBSERVED", "REMAINING_VIOLATION"),
        ("VIOLATION_OBSERVED", "NOT_OBSERVED_COMPLETE", "REMEDIATED_IN_TEST"),
        ("INCONCLUSIVE", "VIOLATION_OBSERVED", "INCONCLUSIVE"),
    ],
)
def test_new_and_remaining_effects_are_distinct(before, after, expected):
    assert (
        comparison_outcome([{"side": "before", "label": before}, {"side": "after", "label": after}])
        == expected
    )


@pytest.mark.parametrize(
    ("operation", "finalize_step"),
    [
        ("request", None),
        ("logs", "services-logs"),
        ("cleanup.json", "cleanup-record"),
        ("before-1.run.json", "run-record"),
    ],
)
def test_interruption_preserves_run_and_cleans_project(
    evaluation, monkeypatch, operation, finalize_step
):
    repo, _, _, _ = evaluation
    docker = OrderDocker(interrupt=operation)
    target = repo / "var" / ("interrupt-" + operation)
    save = order_lookup.save
    interrupted_write = False

    def interrupt_save(path, value):
        nonlocal interrupted_write
        if path.name == operation and not interrupted_write:
            interrupted_write = True
            path.write_bytes(b'{"incomplete":')
            raise KeyboardInterrupt
        save(path, value)

    monkeypatch.setattr(order_lookup, "save", interrupt_save)
    with pytest.raises(KeyboardInterrupt):
        evaluate_order_family(
            repo=repo,
            manifest=repo / ROOT / "family.json",
            output=target,
            execute=True,
            runner=docker,
            sleep=lambda _: None,
        )

    summary = json.loads((target / "summary.json").read_bytes())
    assert summary["status"] == "INTERRUPTED"
    assert summary["allocated_runs"] == ["p0/s0/e/runs/before-1.run.json"]
    record = json.loads((target / summary["allocated_runs"][0]).read_bytes())
    assert record["execution_status"] == ("SUCCEEDED" if finalize_step else "INTERRUPTED")
    assert record["cleanup_status"] == "COMPLETE"
    assert record["extensions"]["flowdelta.request_id"]
    evidence = target / "p0/s0/e/runs/before-1"
    assert json.loads((evidence / "cleanup.json").read_bytes())["status"] == "COMPLETE"
    if finalize_step:
        expected = {
            "phase": "FINALIZING",
            "step": finalize_step,
            "error_code": "INTERRUPTED",
        }
        assert record["extensions"]["flowdelta.finalize_interrupt"] == expected
        assert json.loads((evidence / "finalize-interrupt.json").read_bytes()) == {
            "schema_version": "0.4.0",
            "run_id": record["run_id"],
            **expected,
        }
    assert len(docker.projects) == 1
    assert all(not p.up for p in docker.projects.values())


@pytest.mark.parametrize("mutation", ["request", "events", "state", "missing"])
def test_request_binding_tampering_never_becomes_safe(evaluation, tmp_path, mutation):
    repo, output, _, _ = evaluation
    bundle = Path(shutil.copytree(output / "p0/s1/b", tmp_path / "bundle"))
    # Rebuild artifact digests too: checks must reject semantic mixing, not just stale hashes.
    path = (
        "runs/before-1/"
        + {
            "request": "request.json",
            "events": "events.json",
            "state": "final-state.json",
            "missing": "events.json",
        }[mutation]
    )
    document = json.loads((bundle / path).read_bytes())
    if mutation == "request":
        document["body"]["order_id"] = "ORD-1001"
    elif mutation == "events":
        document["events"][0]["attributes"]["request_id"] = "foreign-request"
    elif mutation == "state":
        document["metadata"]["request_id"] = "foreign-request"
    else:
        document["events"] = []
        document["collection_status"] = "PARTIAL"
    (bundle / path).write_bytes(json_bytes(document))
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    run_path = "runs/before-1.run.json"
    run = json.loads((bundle / run_path).read_bytes())
    for ref in [*manifest["artifacts"], *run["artifacts"]["artifacts"]]:
        if ref["path"] == path:
            ref.update(
                sha256=digest((bundle / path).read_bytes()),
                size_bytes=(bundle / path).stat().st_size,
            )
    (bundle / run_path).write_bytes(json_bytes(run))
    for ref in manifest["artifacts"]:
        if ref["path"] == run_path:
            ref.update(
                sha256=digest((bundle / run_path).read_bytes()),
                size_bytes=(bundle / run_path).stat().st_size,
            )
    (bundle / "manifest.json").write_bytes(json_bytes(manifest))
    report = judge_evaluation_bundle(bundle, repo)
    before = next(r for r in report["records"] if r["side"] == "before")
    assert before["label"] == "INCONCLUSIVE"


@pytest.mark.parametrize("declared", [True, False])
def test_missing_evidence_kind_marks_only_the_affected_run(evaluation, tmp_path, declared):
    repo, output, _, _ = evaluation
    bundle = Path(shutil.copytree(output / "p0/s0/b", tmp_path / "bundle"))
    before_path = "runs/before-1.run.json"
    events_path = "runs/before-1/events.json"
    before = json.loads((bundle / before_path).read_bytes())
    artifacts = before["artifacts"]
    artifacts["artifact_kinds"].pop("events")
    artifacts["observed_kinds"].remove("events")
    artifacts["artifacts"] = [ref for ref in artifacts["artifacts"] if ref["path"] != events_path]
    if declared:
        artifacts.update(missing_kinds=["events"], collection_status="PARTIAL")
    else:
        artifacts["required_kinds"].remove("events")
    run_bytes = json_bytes(before)
    (bundle / before_path).write_bytes(run_bytes)
    (bundle / events_path).unlink()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"] = [ref for ref in manifest["artifacts"] if ref["path"] != events_path]
    for ref in manifest["artifacts"]:
        if ref["path"] == before_path:
            ref.update(sha256=digest(run_bytes), size_bytes=len(run_bytes))
    manifest_path.write_bytes(json_bytes(manifest))

    if not declared:
        with pytest.raises(InputError, match="RUN_REQUIRED_ARTIFACT_MISSING"):
            read_module_outputs(bundle)
    report = judge_evaluation_bundle(bundle, repo)
    records = {record["side"]: record for record in report["records"]}
    assert report["status"] == "INCOMPLETE"
    assert records["before"]["label"] == "INCONCLUSIVE"
    assert records["before"]["reason"] == "REQUIRED_EVIDENCE_MISSING"
    assert records["after"]["label"] == "NOT_OBSERVED_COMPLETE"


def test_missing_referenced_evidence_file_is_a_contract_error(evaluation, tmp_path):
    repo, output, _, _ = evaluation
    bundle = Path(shutil.copytree(output / "p0/s0/b", tmp_path / "bundle"))
    (bundle / "runs/before-1/events.json").unlink()
    with pytest.raises(InputError, match="MISSING_ARTIFACT"):
        judge_evaluation_bundle(bundle, repo)


def test_suite_rejects_changed_registration_before_docker(evaluation):
    repo, _, _, _ = evaluation
    manifest = repo / "var/forged-family.json"
    data = json.loads((repo / ROOT / "family.json").read_bytes())
    data["pairs"] = []
    manifest.write_bytes(json_bytes(data))
    docker = OrderDocker()
    with pytest.raises(InputError, match="ORDER_FAMILY_NOT_REGISTERED"):
        evaluate_order_family(
            repo=repo, manifest=manifest, output=repo / "var/forged", execute=True, runner=docker
        )
    assert not docker.calls


def test_self_consistent_but_unregistered_scenario_is_rejected(evaluation, tmp_path):
    repo, output, _, _ = evaluation
    bundle = Path(shutil.copytree(output / "p0/s1/p", tmp_path / "forged"))
    data = json.loads((bundle / "scenario.json").read_bytes())
    data["request_input"]["order_id"] = "ORD-9999"
    raw = json_bytes(data)
    (bundle / "scenario.json").write_bytes(raw)
    plan = json.loads((bundle / "plan.json").read_bytes())
    plan["extensions"][SCENARIO_KEY].update(sha256=digest(raw), size_bytes=len(raw))
    (bundle / "plan.json").write_bytes(json_bytes(plan))
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    for ref in manifest["artifacts"]:
        if ref["path"] in {"scenario.json", "plan.json"}:
            content = (bundle / ref["path"]).read_bytes()
            ref.update(sha256=digest(content), size_bytes=len(content))
    (bundle / "manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(InputError, match="ORDER_SCENARIO_NOT_REGISTERED"):
        preflight(*read_plan_bundle(bundle), bundle, repo)


def test_relative_bundle_can_be_rejudged_from_cli(evaluation, monkeypatch):
    from typer.testing import CliRunner

    from flowdelta.cli import app

    repo, _, _, _ = evaluation
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        app,
        [
            "judge-order-evaluation-bundle",
            "var/e/p0/s1/b",
            "--out",
            "var/relative-recheck",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((repo / "var/relative-recheck/report.json").read_bytes())
    assert report["status"] == "COMPLETE"


def test_missing_family_is_a_stable_cli_error(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from flowdelta.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "evaluate-order-family",
            "--manifest",
            "absent/family.json",
            "--out",
            "var/e",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "ORDER_FAMILY_UNREADABLE"
