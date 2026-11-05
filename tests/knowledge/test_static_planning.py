import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.graph.analyze import analyze, identified
from flowdelta.ingestion import InputError, digest, json_bytes, parse_file_model
from flowdelta.knowledge.cards import load_cards
from flowdelta.knowledge.planner import make_plan, write_plan
from flowdelta.knowledge.retrieval import analysis_retrieval_context, exclusion_reason
from flowdelta.normalize import normalize
from flowdelta.pipeline_contracts import ImpactPath, RetrievalResult
from flowdelta.pipeline_contracts import TestPlan as PlanContract
from flowdelta.pipeline_io import read_handoff
from flowdelta.runtime import static_cli as static_cli_module
from flowdelta.runtime import static_rules
from flowdelta.runtime.static_adapter import run_static_plan as run_static_plan_adapter
from flowdelta.runtime.static_cli import main as static_cli_main
from flowdelta.runtime.static_scan import (
    SEMGREP_VERSION,
    StaticScanRun,
    registered_catalog,
    validate_static_plan,
)

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "examples/contracts/order-lookup"
CARDS = ROOT / "examples/knowledge/n8n-claw-memory/cards"
AGENT = "@n8n/n8n-nodes-langchain.agent"
TOOL_CODE = "@n8n/n8n-nodes-langchain.toolCode"


def workflow(code: str) -> bytes:
    """Build a small workflow with the C-registered agent tool target shape."""
    return json_bytes(
        {
            "nodes": [
                {
                    "id": "agent",
                    "name": "Agent",
                    "type": AGENT,
                    "typeVersion": 3.1,
                    "parameters": {},
                },
                {
                    "id": "memory-delete",
                    "name": "Memory Delete",
                    "type": TOOL_CODE,
                    "typeVersion": 1.2,
                    "parameters": {"jsCode": code},
                },
            ],
            "connections": {
                "Memory Delete": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]}
            },
        }
    )


def planning_inputs(tmp_path: Path, *, truncated: bool = False):
    """Build a local handoff and A analysis with one controlled tool-code path."""
    handoff = read_handoff(BUNDLE / "input")
    handoff_root = tmp_path / "handoff"
    shutil.copytree(BUNDLE / "input", handoff_root)
    raw_workflows = [workflow("return query;"), workflow("return await remove(query);")]
    snapshots = []
    workflows = {}
    refs = []
    for snapshot, data in zip(handoff.snapshots.snapshots, raw_workflows, strict=True):
        target = handoff_root / snapshot.workflow_ref.path
        target.write_bytes(data)
        ref = snapshot.workflow_ref.model_copy(
            update={"sha256": digest(data), "size_bytes": len(data)}
        )
        snapshots.append(snapshot.model_copy(update={"workflow_ref": ref}))
        refs.append(ref)
        workflows[snapshot.side] = normalize(json.loads(data), ref.artifact_id)
    updated_refs = {ref.path: ref for ref in refs}
    source_refs = [updated_refs.get(ref.path, ref) for ref in handoff.source_manifest.artifacts]
    handoff = handoff.model_copy(
        update={
            "task": handoff.task.model_copy(
                update={"task_id": "n8n-claw-memory", "normal_input": {}}
            ),
            "environment": handoff.environment.model_copy(
                update={
                    "extensions": {
                        **handoff.environment.extensions,
                        "flowdelta.semantic_profile": "core-local-v1",
                    }
                }
            ),
            "before": workflows["before"],
            "after": workflows["after"],
            "snapshots": handoff.snapshots.model_copy(update={"snapshots": snapshots}),
            "source_manifest": handoff.source_manifest.model_copy(
                update={"artifacts": source_refs}
            ),
        }
    )
    analysis = analyze(handoff)
    changed = next(item for item in analysis.changes if item.node_id == "memory-delete")
    analysis.impact_paths = [
        ImpactPath(
            path_id="impact-memory-delete",
            side="after",
            change_ids=[changed.change_id],
            edge_ids=[],
            task_ids=[handoff.task.task_id],
            policy_rule_ids=[],
            support="SUPPORTED",
            limitations=[],
        )
    ]
    if truncated:
        analysis.extensions["flowdelta.graph.limits"] = {
            "truncated": True,
            "reasons": ["IMPACT_PATH_LIMIT"],
            "impact_paths": {"complete": False, "total": None, "kept": 1},
        }
    analysis = identified(analysis)
    catalog = registered_catalog(ROOT)
    return handoff, analysis, load_cards(CARDS), catalog, handoff_root


def fake_semgrep(arguments: list[str], timeout: int) -> tuple[int, bytes]:
    """Return a valid empty report for C's plan handoff boundary test."""
    if arguments == ["--version"]:
        return 0, f"{SEMGREP_VERSION}\n".encode()
    files = sorted(Path(arguments[-1]).glob("*.js"))
    report = {
        "errors": [],
        "results": [],
        "paths": {"scanned": [str(path) for path in files]},
    }
    return 0, json.dumps(report).encode()


def test_flowdelta_plan_accepts_the_registered_static_scan_catalog(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    catalog_path = tmp_path / "static-scan-catalog.json"
    catalog_path.write_bytes(json_bytes(registered_catalog(ROOT).model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "plan",
            str(BUNDLE),
            "--catalog",
            str(catalog_path),
            "--cards",
            str(CARDS),
            "--out",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    plan = parse_file_model(PlanContract, json.loads(output.read_bytes()))
    catalog = registered_catalog(ROOT)
    template = validate_static_plan(plan, catalog, ROOT)
    assert (plan.catalog_id, plan.template_id, plan.template_version) == (
        "static-scan-catalog-v1",
        "static-agent-tool-ownership",
        "0.1.0",
    )
    assert template.timeout_seconds == plan.timeout_seconds == 120
    assert plan.normal_input == {}
    assert plan.planner_metadata["selected_path_id"] is None
    assert plan.planner_metadata["analysis_context"]["analysis_id"] == plan.analysis_id
    assert plan.retrieval_ref is not None
    retrieval = parse_file_model(
        RetrievalResult,
        json.loads((output.parent / plan.retrieval_ref.path).read_bytes()),
    )
    assert plan.retrieval_ref.artifact_id == retrieval.retrieval_id
    assert plan.plan_id == f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}"


def test_static_card_requires_a_c_target_present_in_the_workflow(tmp_path: Path) -> None:
    handoff, _, cards, catalog, _ = planning_inputs(tmp_path)
    card = cards[0].model_copy(update={"node_types": [AGENT, TOOL_CODE]})
    templates = {(item.template_id, item.version): item for item in catalog.templates}

    reason = exclusion_reason(
        card,
        handoff,
        templates,
        "core-local-v1",
        {AGENT},
    )

    assert reason == "NODE_TYPE_MISMATCH"


def test_c_cli_reads_the_exact_b_plan_without_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "plan.json"
    catalog_path = tmp_path / "static-scan-catalog.json"
    catalog_path.write_bytes(json_bytes(registered_catalog(ROOT).model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "plan",
            str(BUNDLE),
            "--catalog",
            str(catalog_path),
            "--cards",
            str(CARDS),
            "--out",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    original_bytes = output.read_bytes()
    original_plan = parse_file_model(PlanContract, json.loads(original_bytes))

    repo = tmp_path / "repo"
    rule = repo / static_rules.RULE_PATH
    rule.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / static_rules.RULE_PATH, rule)
    (repo / "var").mkdir()

    def run_with_fake_semgrep(handoff, plan, catalog, handoff_root, repo_root, output):
        """Run C's adapter with deterministic Semgrep output for the CLI handoff check."""
        return run_static_plan_adapter(
            handoff,
            plan,
            catalog,
            handoff_root,
            repo_root,
            output,
            fake_semgrep,
            lambda: "2026-11-05T00:00:00Z",
        )

    monkeypatch.setattr(static_cli_module, "run_static_plan", run_with_fake_semgrep)
    monkeypatch.chdir(repo)
    exit_code = static_cli_main(
        [
            "--handoff",
            str(BUNDLE / "input"),
            "--plan",
            str(output),
            "--catalog",
            str(catalog_path),
            "--out",
            "var/static-run",
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 2, summary["runs"]
    assert summary["plan_id"] == original_plan.plan_id
    for side in ("before", "after"):
        run = parse_file_model(
            StaticScanRun,
            json.loads((repo / "var/static-run/runs" / f"{side}-1.static-run.json").read_bytes()),
        )
        assert run.plan_id == original_plan.plan_id
        assert (run.status, run.error_code) == ("NOT_RUN", "NO_SUPPORTED_TARGET")
    assert output.read_bytes() == original_bytes


def test_unique_supported_path_is_selected_and_all_analysis_context_is_kept(
    tmp_path: Path,
) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)

    plan = make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)

    context = plan.planner_metadata["analysis_context"]
    assert plan.planner_metadata["selected_path_id"] == "impact-memory-delete"
    assert plan.planner_metadata["path_selection"]["support"] == "SUPPORTED"
    assert context["impact_paths"] == [
        {
            "path_id": path.path_id,
            "side": path.side,
            "change_ids": path.change_ids,
            "edge_ids": path.edge_ids,
            "task_ids": path.task_ids,
            "policy_rule_ids": path.policy_rule_ids,
            "support": path.support,
            "basis": path.basis,
            "limitations": path.limitations,
        }
        for path in analysis.impact_paths
    ]
    assert context["changes"]
    assert context["analysis_id"] == analysis.analysis_id
    assert context["analysis_content_sha256"]
    assert context == analysis_retrieval_context(handoff, analysis)[0]


def test_ambiguous_paths_need_an_explicit_path_id(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.impact_paths.append(
        analysis.impact_paths[0].model_copy(update={"path_id": "impact-also"})
    )
    analysis = identified(analysis)

    unresolved = make_plan(
        handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT
    )
    explicit = make_plan(
        handoff,
        analysis,
        cards,
        catalog,
        handoff_root=handoff_root,
        repo_root=ROOT,
        selected_path_id="impact-also",
    )

    assert unresolved.planner_metadata["selected_path_id"] is None
    assert unresolved.planner_metadata["path_selection"]["status"] == "AMBIGUOUS"
    assert explicit.planner_metadata["selected_path_id"] == "impact-also"
    assert explicit.planner_metadata["path_selection"]["status"] == "EXPLICIT"


def test_truncated_analysis_keeps_supported_path_uncertain(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path, truncated=True)

    plan = make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)

    assert plan.planner_metadata["selected_path_id"] is None
    assert plan.planner_metadata["path_selection"]["status"] == "TRUNCATED"
    assert plan.planner_metadata["analysis_context"]["graph_limits"]["truncated"] is True


def test_explicit_supported_path_does_not_clear_truncation(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path, truncated=True)

    plan = make_plan(
        handoff,
        analysis,
        cards,
        catalog,
        handoff_root=handoff_root,
        repo_root=ROOT,
        selected_path_id="impact-memory-delete",
    )

    assert plan.planner_metadata["selected_path_id"] == "impact-memory-delete"
    assert plan.planner_metadata["path_selection"]["status"] == "EXPLICIT_LIMITED"
    assert plan.planner_metadata["path_selection"]["graph_limits"]["truncated"] is True
    assert plan.planner_metadata["security_outcome"] is None


def test_missing_path_stays_uncertain(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.impact_paths = []
    analysis = identified(analysis)

    plan = make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)

    assert plan.planner_metadata["selected_path_id"] is None
    assert plan.planner_metadata["path_selection"]["status"] == "MISSING"


def test_unsupported_path_is_not_selected(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.impact_paths[0] = analysis.impact_paths[0].model_copy(
        update={"support": "UNKNOWN", "limitations": ["tool semantics are unknown"]}
    )
    analysis = identified(analysis)

    plan = make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)

    assert plan.planner_metadata["selected_path_id"] is None
    assert plan.planner_metadata["path_selection"]["status"] == "UNSUPPORTED"
    assert plan.planner_metadata["analysis_context"]["impact_paths"][0]["limitations"] == [
        "tool semantics are unknown"
    ]


def test_explicit_path_must_belong_to_the_analysis_and_match_a_c_target(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)

    with pytest.raises(InputError, match="STATIC_PATH_NOT_FOUND"):
        make_plan(
            handoff,
            analysis,
            cards,
            catalog,
            handoff_root=handoff_root,
            repo_root=ROOT,
            selected_path_id="from-another-analysis",
        )


def test_analysis_must_match_the_handoff_case(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.case_id = "another-case"

    with pytest.raises(InputError, match="CASE_ID_MISMATCH"):
        make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)


def test_static_analysis_graph_connections_must_match_the_handoff(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.before_graph.edges = []
    analysis = identified(analysis)

    with pytest.raises(InputError, match="GRAPH_CONTROL_COVERAGE_MISMATCH"):
        make_plan(handoff, analysis, cards, catalog, handoff_root=handoff_root, repo_root=ROOT)


def test_path_without_a_supported_c_target_cannot_be_selected(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    change = next(item for item in analysis.changes if item.node_id == "memory-delete")
    analysis.changes = [
        item.model_copy(update={"node_id": "agent"}) if item.change_id == change.change_id else item
        for item in analysis.changes
    ]
    analysis = identified(analysis)

    with pytest.raises(InputError, match="STATIC_PATH_NOT_ELIGIBLE"):
        make_plan(
            handoff,
            analysis,
            cards,
            catalog,
            handoff_root=handoff_root,
            repo_root=ROOT,
            selected_path_id="impact-memory-delete",
        )


def test_static_path_selection_request_changes_b_artifact_identity(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    analysis.impact_paths.append(
        analysis.impact_paths[0].model_copy(update={"path_id": "impact-also"})
    )
    analysis = identified(analysis)
    requests = (None, "impact-memory-delete", "impact-also")
    plans = []
    plan_bytes = []

    for index, selected_path_id in enumerate(requests):
        output_dir = tmp_path / f"plan-{index}"
        output_dir.mkdir()
        output = output_dir / "plan.json"
        plan = write_plan(
            handoff,
            analysis,
            cards,
            catalog,
            output,
            handoff_root=handoff_root,
            repo_root=ROOT,
            selected_path_id=selected_path_id,
        )
        plans.append(plan)
        plan_bytes.append(output.read_bytes())

    assert len({plan.plan_id for plan in plans}) == len(requests)
    assert len({plan.retrieval_ref.artifact_id for plan in plans}) == len(requests)
    assert len(set(plan_bytes)) == len(requests)
    assert all(
        plan.plan_id == f"plan-{plan.retrieval_ref.artifact_id.removeprefix('retrieval-')}"
        for plan in plans
    )


def test_static_catalog_with_contract_example_authorship_is_rejected(tmp_path: Path) -> None:
    handoff, analysis, cards, catalog, handoff_root = planning_inputs(tmp_path)
    contract_example_catalog = catalog.model_copy(
        update={"authorship": catalog.authorship.model_copy(update={"origin": "CONTRACT_EXAMPLE"})}
    )

    with pytest.raises(InputError, match="STATIC_CATALOG_NOT_REGISTERED"):
        make_plan(
            handoff,
            analysis,
            cards,
            contract_example_catalog,
            handoff_root=handoff_root,
            repo_root=ROOT,
        )
