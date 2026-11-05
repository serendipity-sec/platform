import json
import shutil
from pathlib import Path

import pytest
from test_pipeline_runner import inputs
from typer.testing import CliRunner

from flowdelta import pipeline_runner
from flowdelta.cli import app
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.order_verification import verify_profiled_order
from flowdelta.pipeline_runner import run_pipeline
from flowdelta.runtime.order_lookup import CommandResult

from .fake_docker import FakeDocker

REPO = Path(__file__).parents[2]


class OrderDocker(FakeDocker):
    def probe(self, project, payload, timeout):
        result = super().probe(project, payload, timeout)
        if payload["operation"] == "normal-request":
            project.events[-1].update(resource_id="ORD-1001", outcome="RETURNED")
            response = json.loads(result.stdout)
            if len(self.requests) % 2 == 0:
                response["body"]["display_status"] = "주문 상태: 준비중"
            return CommandResult(result.returncode, json.dumps(response), result.stderr)
        return result


@pytest.fixture
def options(tmp_path):
    configured = inputs(tmp_path)
    shutil.copytree(REPO / "runtime", tmp_path / "runtime")
    shutil.copytree(REPO / "src/flowdelta/baseline", tmp_path / "src/flowdelta/baseline")
    return configured


def test_execute_real_orchestration_with_injected_commands_and_verifier(options):
    docker = OrderDocker()
    summary = run_pipeline(**options, execute=True, runner=docker, sleep=lambda _: None)
    assert summary["status"] == "EXECUTED"
    assert summary["security_outcome"] is None
    assert len(summary["allocated_runs"]) == len(summary["runs"]) == 2
    assert verify_profiled_order(options["output"], REPO)["passed"]
    assert len(docker.requests) == 2


@pytest.mark.parametrize("failures", [{"normal-request": "timeout"}, {"down": "fail"}])
def test_execution_failures_remain_incomplete_with_error_context(options, failures):
    result = run_pipeline(
        **options, execute=True, runner=FakeDocker(failures=failures), sleep=lambda _: None
    )
    assert result["status"] == "INCOMPLETE"
    assert result["stages"]["execution"] == "INCOMPLETE"
    assert result["contract_validation"]["run_count"] == 2
    assert all("error_code" in run and "failed_phase" in run for run in result["runs"])
    with pytest.raises(InputError, match="EXECUTION_NOT_COMPLETE"):
        verify_profiled_order(options["output"], REPO)


def test_cli_execute_incomplete_returns_two(options, monkeypatch):
    real = pipeline_runner.run_pipeline
    monkeypatch.chdir(options["repo"])
    monkeypatch.setattr(
        pipeline_runner,
        "run_pipeline",
        lambda **kwargs: real(
            **kwargs, runner=FakeDocker(response_status=500), sleep=lambda _: None
        ),
    )
    args = ["run-pipeline", str(options["case"]), "--execute"]
    for key in ("profile", "profile_sha256", "catalog", "cards", "output"):
        args.extend(
            ["--out" if key == "output" else "--" + key.replace("_", "-"), str(options[key])]
        )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["status"] == "INCOMPLETE"


def test_summary_io_failure_preserves_original_input_error(options, monkeypatch):
    options["profile_sha256"] = "0" * 64

    def fail(*args):
        raise OSError("summary disk failure")

    monkeypatch.setattr(pipeline_runner, "_save_summary", fail)
    with pytest.raises(InputError, match="PROFILE_IDENTITY_MISMATCH") as caught:
        run_pipeline(**options)
    assert "PIPELINE_SUMMARY_WRITE_FAILED" in caught.value.__notes__


def test_final_bundle_uses_executed_plan_not_mutable_producer(options, monkeypatch):
    original = pipeline_runner.PlanExecutor.execute_plan

    def mutate(executor, *args):
        runs = original(executor, *args)
        (options["output"] / "producers/analysis.json").write_text("changed after execution")
        return runs

    monkeypatch.setattr(pipeline_runner.PlanExecutor, "execute_plan", mutate)
    result = run_pipeline(**options, execute=True, runner=OrderDocker(), sleep=lambda _: None)
    assert result["status"] == "EXECUTED"
    for name in ("analysis.json", "plan.json", "retrieval.json"):
        assert (options["output"] / "bundle" / name).read_bytes() == (
            options["output"] / "plan-bundle" / name
        ).read_bytes()


def test_mutated_executed_bundle_is_rejected(options, monkeypatch):
    original = pipeline_runner.PlanExecutor.execute_plan

    def mutate(executor, *args):
        runs = original(executor, *args)
        (executor.bundle_root / "analysis.json").write_text("tampered")
        return runs

    monkeypatch.setattr(pipeline_runner.PlanExecutor, "execute_plan", mutate)
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        run_pipeline(**options, execute=True, runner=OrderDocker(), sleep=lambda _: None)


def rewrite_member(root, relative, change):
    path = root / relative
    value = json.loads(path.read_bytes())
    change(value)
    path.write_bytes(json_bytes(value))
    return {"sha256": digest(path.read_bytes()), "size_bytes": path.stat().st_size}


@pytest.mark.parametrize("field,value", [("resource_id", "OTHER"), ("outcome", "DENIED")])
def test_semantically_wrong_events_fail_even_with_consistent_hashes(options, field, value):
    run_pipeline(**options, execute=True, runner=OrderDocker(), sleep=lambda _: None)
    root = options["output"] / "bundle"
    event_name = "runs/before-1/events.json"
    event_ref = rewrite_member(
        root, event_name, lambda doc: doc["events"][0].update({field: value})
    )
    run_name = "runs/before-1.run.json"
    run_ref = rewrite_member(
        root,
        run_name,
        lambda doc: [
            ref.update(event_ref)
            for ref in doc["artifacts"]["artifacts"]
            if ref["path"] == event_name
        ],
    )
    rewrite_member(
        root,
        "manifest.json",
        lambda doc: [
            ref.update({event_name: event_ref, run_name: run_ref}[ref["path"]])
            for ref in doc["artifacts"]
            if ref["path"] in (event_name, run_name)
        ],
    )
    rewrite_member(
        options["output"],
        "summary.json",
        lambda doc: doc.update(
            bundle_manifest_sha256=digest((root / "manifest.json").read_bytes())
        ),
    )
    with pytest.raises(InputError, match="NORMAL_BUSINESS_CHECK_FAILED"):
        verify_profiled_order(options["output"], REPO)


def test_verifier_rejects_raw_artifact_tampering_and_forged_summary(options):
    run_pipeline(**options, execute=True, runner=OrderDocker(), sleep=lambda _: None)
    path = options["output"] / "summary.json"
    original = path.read_bytes()
    rewrite_member(
        options["output"],
        "summary.json",
        lambda doc: doc["runs"][0].update(run_id="run-fabricated"),
    )
    with pytest.raises(InputError, match="COMPARISON_RUN_IDENTITIES_INVALID"):
        verify_profiled_order(options["output"], REPO)
    path.write_bytes(original)
    (options["output"] / "bundle/runs/before-1/events.json").write_text("{}")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        verify_profiled_order(options["output"], REPO)


def test_analysis_limits_are_forwarded_without_invented_defaults(options, monkeypatch):
    original = pipeline_runner.analyze
    limits = {"truncated": True, "omitted_count": 3}

    def bounded(handoff):
        result = original(handoff)
        result.extensions["flowdelta.graph.limits"] = limits
        return result

    monkeypatch.setattr(pipeline_runner, "analyze", bounded)
    assert run_pipeline(**options)["analysis_graph_limits"] == limits
