"""Regression boundaries reported on PR #56."""

import json

import pytest
from conftest import edit_document
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.fixed_profile import freeze_profile, validate_profile
from flowdelta.ingestion import InputError, load_case
from flowdelta.pipeline_io import read_handoff


def add_node(bundle, node_type, port=None):
    for side in ("before", "after"):

        def change(workflow):
            workflow["nodes"].append(
                {
                    "id": "provider",
                    "name": "Provider",
                    "type": node_type,
                    "typeVersion": 1,
                    "parameters": {"model": "fixed-model"},
                    "position": [0, 0],
                }
            )
            if port:
                workflow["connections"]["Provider"] = {
                    port: [[{"node": workflow["nodes"][0]["name"], "type": port, "index": 0}]]
                }

        edit_document(bundle, f"workflow/{side}.json", change)


@pytest.mark.parametrize(
    "node_type,port",
    [
        ("@n8n/n8n-nodes-langchain.openAi", None),
        ("@n8n/n8n-nodes-langchain.anthropic", None),
        ("@n8n/n8n-nodes-langchain.googleGemini", None),
        ("n8n-nodes-base.openAi", None),
        ("@n8n/n8n-nodes-langchain.embeddingsOpenAi", None),
        ("custom.embedding", "ai_embedding"),
        ("custom.reranker", "ai_reranker"),
    ],
)
def test_direct_and_auxiliary_models_cannot_drift(bundle, node_type, port):
    add_node(bundle, node_type, port)
    profile = freeze_profile(load_case(bundle), profile_id="p", revision="1")
    edit_document(
        bundle,
        "workflow/after.json",
        lambda w: w["nodes"][-1]["parameters"].update(model="changed"),
    )
    with pytest.raises(InputError, match="PROFILE_MODEL_CONFIGURATION_CHANGED"):
        validate_profile(load_case(bundle), profile)


def test_candidate_mcp_client_is_not_an_unchanged_dependency(bundle):
    profile = freeze_profile(load_case(bundle), profile_id="p", revision="1")
    edit_document(
        bundle,
        "workflow/after.json",
        lambda w: w["nodes"].append(
            {
                "id": "mcp",
                "name": "MCP",
                "type": "@n8n/n8n-nodes-langchain.mcpClientTool",
                "typeVersion": 1,
                "parameters": {"sseEndpoint": "https://fixture.invalid/sse"},
                "position": [0, 0],
            }
        ),
    )
    with pytest.raises(InputError, match="PROFILE_MCP_DEPENDENCY_UNSUPPORTED"):
        validate_profile(load_case(bundle), profile)


def test_additional_fixed_node_rejects_runtime_configuration(bundle):
    with pytest.raises(InputError, match="PROFILE_DYNAMIC_FIXED_CONFIGURATION"):
        freeze_profile(
            load_case(bundle),
            profile_id="p",
            revision="1",
            additional_fixed_node_ids=("read-order",),
        )


def test_cli_profile_receipt_survives_handoff_round_trip(bundle, tmp_path):
    cli = CliRunner()
    path = tmp_path / "profile.json"
    result = cli.invoke(
        app,
        ["freeze-profile", str(bundle), "--out", str(path), "--profile-id", "p", "--revision", "1"],
    )
    assert result.exit_code == 0, result.output
    pin = json.loads(result.output)["profile_sha256"]
    output = tmp_path / "handoff"
    result = cli.invoke(
        app,
        [
            "profile-snapshot",
            str(bundle),
            "--profile",
            str(path),
            "--profile-sha256",
            pin,
            "--out",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    handoff = read_handoff(output)
    receipt = handoff.snapshots.extensions["flowdelta.fixed_profile"]
    assert receipt["profile_sha256"] == pin
    assert receipt["case_id"] == handoff.case.case_id
    assert receipt["before_snapshot_id"] == handoff.snapshots.snapshots[0].snapshot_id
    assert receipt["after_snapshot_id"] == handoff.snapshots.snapshots[1].snapshot_id
    assert receipt["execution_status"] == "NOT_RUN"


def test_cli_cannot_publish_profile_inside_input(bundle):
    target = bundle / "profile.json"
    result = CliRunner().invoke(
        app,
        [
            "freeze-profile",
            str(bundle),
            "--out",
            str(target),
            "--profile-id",
            "p",
            "--revision",
            "1",
        ],
    )
    assert result.exit_code == 2
    assert "OUTPUT_OVERLAPS_INPUT" in result.output
    assert not target.exists()


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_failed_profile_write_is_retryable(bundle, tmp_path, monkeypatch, failure):
    from flowdelta import artifact_io
    from flowdelta.fixed_profile import write_profile

    profile = freeze_profile(load_case(bundle), profile_id="p", revision="1")
    target = tmp_path / "profile.json"
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("injected failure")

        patch.setattr(artifact_io.os, failure, fail)
        with pytest.raises(InputError, match="ARTIFACT_WRITE_ERROR"):
            write_profile(profile, target, input_root=bundle)
    assert not target.exists()
    assert not list(tmp_path.glob(".flowdelta-file-*"))
    write_profile(profile, target, input_root=bundle)
    assert json.loads(target.read_bytes())["profile_id"] == "p"


@pytest.mark.parametrize(
    "fixed_nodes,code",
    [
        (["absent"], "PROFILE_FIXED_NODE_MISSING"),
        (["read-order", "read-order"], "PROFILE_DUPLICATE_FIXED_NODE"),
    ],
)
def test_cli_fixed_node_errors(bundle, tmp_path, fixed_nodes, code):
    args = [
        "freeze-profile",
        str(bundle),
        "--profile-id",
        "p",
        "--revision",
        "1",
        "--out",
        str(tmp_path / "p.json"),
    ]
    for node in fixed_nodes:
        args.extend(["--fixed-node", node])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert code in result.output


def test_check_profile_does_not_build_semantic_handoff(bundle, tmp_path, monkeypatch):
    from flowdelta import fixed_profile

    profile = freeze_profile(load_case(bundle), profile_id="p", revision="1")
    path = tmp_path / "p.json"
    fixed_profile.write_profile(profile, path, input_root=bundle)

    def forbidden(*args):
        pytest.fail("check-profile must not build the semantic handoff")

    monkeypatch.setattr(fixed_profile, "build_handoff", forbidden)
    result = CliRunner().invoke(
        app,
        [
            "check-profile",
            str(bundle),
            "--profile",
            str(path),
            "--profile-sha256",
            fixed_profile.profile_digest(profile),
        ],
    )
    assert result.exit_code == 0, result.output


def test_profile_missing_parent_is_a_safe_read_error(tmp_path):
    from flowdelta.fixed_profile import read_profile

    with pytest.raises(InputError, match="PROFILE_READ_ERROR"):
        read_profile(tmp_path / "absent" / "p.json", expected_sha256="0" * 64)
