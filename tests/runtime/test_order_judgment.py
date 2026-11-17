import json
import shutil

import pytest
from conftest import edit_document
from test_pipeline_runner import inputs
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.fixed_profile import freeze_profile, profile_digest, write_profile
from flowdelta.ingestion import InputError, digest, json_bytes, load_case
from flowdelta.order_judgment import judge_order_bundle, publish_order_judgments
from flowdelta.pipeline_runner import run_pipeline
from flowdelta.runtime.order_lookup import CommandResult

from .test_pipeline_execution import REPO, OrderDocker


@pytest.fixture
def options(tmp_path):
    configured = inputs(tmp_path)
    shutil.copytree(REPO / "runtime", tmp_path / "runtime")
    shutil.copytree(REPO / "src/flowdelta/baseline", tmp_path / "src/flowdelta/baseline")
    return configured


class ObservedOrderDocker(OrderDocker):
    """Protocol-faithful independent fixture observations; no security labels are substituted."""

    def __init__(self, mode="normal", **kwargs):
        super().__init__(**kwargs)
        self.mode = mode

    def probe(self, project, payload, timeout):
        result = super().probe(project, payload, timeout)
        if payload["operation"] == "normal-request":
            project.events[-1].update(
                producer="orders-api", principal="customer-demo", tenant="tenant-demo"
            )
            if self.mode in {"foreign", "denied", "forged-initial-owner"}:
                project.events[-1]["resource_id"] = "ORD-2001"
            if self.mode == "denied":
                project.events[-1]["outcome"] = "NOT_FOUND"
            if self.mode == "no-call":
                project.events.clear()
            if self.mode == "wrong-actor":
                project.events[-1]["principal"] = "other-demo"
        if payload["operation"] == "state" and result.returncode == 0:
            response = json.loads(result.stdout)
            doc = response["body"]
            doc["orders"] = json.loads(
                (REPO / "examples/order-lookup/state/initial.json").read_bytes()
            )["data"]["orders"]
            if self.mode == "forged-initial-owner":
                doc["orders"][1]["customer_id"] = "customer-demo"
                doc["orders"][1]["tenant_id"] = "tenant-demo"
            doc["orders_sha256"] = digest(json_bytes(doc["orders"]))
            if self.mode == "mixed-state" and project.events:
                doc["metadata"]["run_id"] = "run-unrelated"
            return CommandResult(0, json.dumps(response), "")
        return result


@pytest.mark.parametrize(
    "mode,label,status",
    [
        ("normal", "NOT_OBSERVED_COMPLETE", "EXECUTED"),
        ("foreign", "VIOLATION_OBSERVED", "EXECUTED"),
        ("denied", "NOT_OBSERVED_COMPLETE", "EXECUTED"),
        ("no-call", "INCONCLUSIVE", "INCOMPLETE"),
        ("wrong-actor", "INCONCLUSIVE", "INCOMPLETE"),
        ("mixed-state", "INCONCLUSIVE", "INCOMPLETE"),
        ("forged-initial-owner", "INCONCLUSIVE", "INCOMPLETE"),
    ],
)
def test_pipeline_publishes_independent_judgments_and_preserves_execution(
    options, mode, label, status
):
    summary = run_pipeline(
        **options,
        execute=True,
        judge_order=True,
        runner=ObservedOrderDocker(mode),
        sleep=lambda _: None,
    )
    assert summary["status"] == status
    assert summary["security_outcome"] is None
    assert summary["stages"]["execution"] == "SUCCEEDED"
    assert [run["label"] for run in summary["judgment"]["runs"]] == [label, label]
    report_path = options["output"] / summary["judgment"]["report"]
    assert digest(report_path.read_bytes()) == summary["judgment"]["sha256"]
    report = json.loads(report_path.read_bytes())
    bundle = options["output"] / "bundle"
    assert report["bundle_manifest_sha256"] == digest((bundle / "manifest.json").read_bytes())
    assert report["request_binding"]["principal"] == "customer-demo"
    assert len(report["records"]) == 2
    for record in report["records"]:
        assert record["judgment"]["outcome"] is None
        assert record["source_artifacts"]
        if mode == "normal":
            assert record["input"]["plan_kind"] == "order-read"
            assert record["judgment"]["allowed_effects_observed"] == 1
    assert all(
        json.loads(path.read_bytes())["outcome"] is None
        for path in (bundle / "runs").glob("*.run.json")
    )


def test_requested_judgment_without_execution_is_rejected_before_allocation(options):
    with pytest.raises(InputError, match="JUDGMENT_REQUIRES_EXECUTION"):
        run_pipeline(**options, judge_order=True)
    assert not options["output"].exists()


def test_partial_evidence_never_becomes_a_complete_judgment(options):
    summary = run_pipeline(
        **options,
        execute=True,
        judge_order=True,
        runner=ObservedOrderDocker(failures={"final-state": "fail"}),
        sleep=lambda _: None,
    )
    assert summary["status"] == "INCOMPLETE"
    assert all(run["label"] == "INCONCLUSIVE" for run in summary["judgment"]["runs"])


def test_posthoc_cli_reuses_verified_bundle_and_does_not_overwrite(options, monkeypatch):
    run_pipeline(**options, execute=True, runner=ObservedOrderDocker(), sleep=lambda _: None)
    bundle = options["output"] / "bundle"
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    monkeypatch.chdir(options["repo"])
    out = options["repo"] / "var" / "posthoc"
    result = CliRunner().invoke(app, ["judge-order-bundle", str(bundle), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "COMPLETE"
    relative = CliRunner().invoke(
        app,
        [
            "judge-order-bundle",
            str(bundle.relative_to(options["repo"])),
            "--out",
            "var/relative-replay",
        ],
    )
    assert relative.exit_code == 0, relative.output
    assert before == {
        p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
    }
    with pytest.raises(InputError, match="JUDGMENT_OUTPUT_WRITE_FAILED"):
        publish_order_judgments(bundle, options["repo"], out)


def test_artifact_hash_tampering_is_refused_before_labeling(options):
    run_pipeline(**options, execute=True, runner=ObservedOrderDocker(), sleep=lambda _: None)
    bundle = options["output"] / "bundle"
    (bundle / "runs/before-1/events.json").write_bytes(b"{}")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        judge_order_bundle(bundle, options["repo"])


def test_prepared_bundle_has_no_security_verdict(options):
    run_pipeline(**options)
    report = judge_order_bundle(options["output"] / "bundle", options["repo"])
    assert report["status"] == "NOT_RUN" and report["records"] == []


def test_a_newly_frozen_policy_cannot_reuse_the_registered_request_binding(options):
    edit_document(
        options["case"],
        "policy.json",
        lambda policy: policy["rules"][0].update(subject="other-demo / other-tenant"),
    )
    profile = freeze_profile(load_case(options["case"]), profile_id="changed-policy", revision="1")
    options["profile"] = options["repo"] / "changed-profile.json"
    options["profile_sha256"] = profile_digest(profile)
    write_profile(profile, options["profile"], input_root=options["case"])
    with pytest.raises(InputError, match="ORDER_JUDGMENT_POLICY_NOT_REGISTERED"):
        run_pipeline(
            **options,
            execute=True,
            judge_order=True,
            runner=ObservedOrderDocker(),
            sleep=lambda _: None,
        )
    assert not (options["output"] / "judgments").exists()
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["failed_stage"] == "judgment" and summary["security_outcome"] is None
