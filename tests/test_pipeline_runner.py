import json
import shutil
from pathlib import Path

import pytest
from conftest import EXAMPLE, edit_document
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.fixed_profile import freeze_profile, profile_digest, write_profile
from flowdelta.ingestion import InputError, load_case
from flowdelta.module_outputs import read_module_outputs
from flowdelta.pipeline_runner import run_pipeline


def inputs(tmp_path, example="order-lookup"):
    source = Path(shutil.copytree(EXAMPLE.parent / example, tmp_path / "input"))
    profile = freeze_profile(load_case(source), profile_id="test", revision="1")
    profile_path = tmp_path / "profile.json"
    write_profile(profile, profile_path, input_root=source)
    reference = EXAMPLE.parent / "contracts" / example
    return dict(
        repo=tmp_path,
        case=source,
        profile=profile_path,
        profile_sha256=profile_digest(profile),
        catalog=reference / "template-catalog.json",
        cards=reference / "cards",
        output=tmp_path / "var" / "pipeline",
    )


@pytest.mark.parametrize("example", ["order-lookup", "support-triage"])
def test_prepare_two_families_uses_real_a_b_and_preserves_not_run(tmp_path, monkeypatch, example):
    from flowdelta import pipeline_runner

    def no_runtime(*args):
        raise AssertionError("prepare must not construct C executor")

    monkeypatch.setattr(pipeline_runner, "PlanExecutor", no_runtime)
    options = inputs(tmp_path, example)
    result = run_pipeline(**options)
    assert result["status"] == "PREPARED"
    assert result["stages"]["execution"] == "NOT_RUN"
    assert result["security_outcome"] is None
    assert result["analysis_origin"] == result["plan_origin"] == "MODULE_OUTPUT"
    assert (
        read_module_outputs(options["output"] / "bundle")["runtime_record_coverage"]
        == "NOT_SUPPLIED"
    )
    assert json.loads((options["output"] / "summary.json").read_bytes()) == result


def test_profile_failure_leaves_failed_stage_without_a_b_or_c(tmp_path):
    options = inputs(tmp_path)
    edit_document(options["case"], "policy.json", lambda v: v.update(policy_version="changed"))
    with pytest.raises(InputError, match="PROFILE_FIXED_INPUT_CHANGED"):
        run_pipeline(**options)
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["status"] == "FAILED"
    assert summary["stages"]["input"] == "FAILED"
    assert summary["stages"]["analysis"] == summary["stages"]["execution"] == "NOT_RUN"
    assert not (options["output"] / "bundle").exists()


def test_missing_kb_evidence_is_not_reported_as_success(tmp_path):
    options = inputs(tmp_path)
    options["cards"] = tmp_path / "missing"
    with pytest.raises(InputError, match="CARD_DIRECTORY_INVALID"):
        run_pipeline(**options)
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["stages"]["plan"] == "FAILED"
    assert summary["stages"]["execution"] == "NOT_RUN"


def test_unregistered_template_never_bypasses_c_preflight(tmp_path):
    options = inputs(tmp_path, "support-triage")
    with pytest.raises(InputError, match="^TEMPLATE_NOT_REGISTERED$"):
        run_pipeline(**options, execute=True)
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["stages"]["plan"] == "SUCCEEDED"
    assert summary["stages"]["execution"] == "FAILED"
    assert not (options["output"] / "execution").exists()


def test_existing_output_and_output_outside_var_are_rejected(tmp_path):
    options = inputs(tmp_path)
    run_pipeline(**options)
    with pytest.raises(InputError, match="OUTPUT_ALREADY_EXISTS"):
        run_pipeline(**options)
    options["output"] = tmp_path / "elsewhere"
    with pytest.raises(InputError, match="OUTPUT_MUST_BE_UNDER_VAR"):
        run_pipeline(**options)


def test_cli_prepares_with_explicit_inputs(tmp_path, monkeypatch):
    options = inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "run-pipeline",
            str(options["case"]),
            "--profile",
            str(options["profile"]),
            "--profile-sha256",
            options["profile_sha256"],
            "--catalog",
            str(options["catalog"]),
            "--cards",
            str(options["cards"]),
            "--out",
            str(options["output"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "PREPARED"


def test_unexpected_stage_failure_is_recorded_without_input_values(tmp_path, monkeypatch):
    from flowdelta import pipeline_runner

    def broken(*args):
        raise ValueError("private-input-must-not-appear")

    monkeypatch.setattr(pipeline_runner, "analyze", broken)
    options = inputs(tmp_path)
    with pytest.raises(InputError, match="PIPELINE_INTERNAL_ERROR"):
        run_pipeline(**options)
    data = (options["output"] / "summary.json").read_text()
    assert "private-input-must-not-appear" not in data
    assert json.loads(data)["stages"]["analysis"] == "FAILED"


def test_private_parent_of_workspace_is_allowed(tmp_path):
    workspace = tmp_path / "private" / "workspace"
    workspace.mkdir(parents=True)
    assert run_pipeline(**inputs(workspace))["status"] == "PREPARED"


def test_interrupt_is_not_reported_as_not_run(tmp_path, monkeypatch):
    from flowdelta import pipeline_runner

    class InterruptedExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def execute_plan(self, *args):
            raise KeyboardInterrupt

    monkeypatch.setattr(pipeline_runner, "PlanExecutor", InterruptedExecutor)
    options = inputs(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(**options, execute=True)
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["status"] == "INTERRUPTED"
    assert summary["stages"]["execution"] == "INTERRUPTED"
    assert summary["failed_stage"] == "execution"


def test_code_provenance_covers_transitive_execution_sources(tmp_path):
    result = run_pipeline(**inputs(tmp_path))
    paths = result["source_hashes"]
    assert "src/flowdelta/runtime/preflight.py" in paths
    assert "src/flowdelta/graph/data_flow.py" in paths
    assert "src/flowdelta/knowledge/retrieval.py" in paths
