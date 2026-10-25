"""Write the static-scan reference files B and reviewers read (`examples/static-scan/`).

The catalog is generated from the registered templates. The plan, invalid plans and runs are
hand-written contract examples for the n8n-claw first-fix pair (4d1b1b7 -> eb6d7bc). Finding
values come from the tool-fit run in `experiments/n8n-claw-c`; raw reports contain original
code and stay under local `var/`, so only their hash and size appear here.
"""

from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import ArtifactRef
from flowdelta.ingestion import json_bytes
from flowdelta.pipeline_contracts import Authorship, ObligationBinding, TestPlan
from flowdelta.runtime.static_scan import (
    StaticFinding,
    StaticScanRun,
    UnsupportedTarget,
    registered_catalog,
)

EXAMPLE_AUTHOR = Authorship(
    origin="CONTRACT_EXAMPLE",
    producer="flowdelta.runtime.static_examples",
    note="C가 작성한 기대 입출력 예시. B가 생성한 plan이나 실제 C 실행 기록이 아니다.",
)

# Pinned n8n-claw originals (sources in experiments/n8n-claw-c/sources.json).
BEFORE_SHA = "5f297ee69e45fba5b810ce41899cb4b581d74675dbba004d78a979538feb8fc2"  # 4d1b1b7
AFTER_SHA = "c3abd08547a9db5503310a7a3056b0fe49ab700d96aa1b6d78801d260cd885aa"  # eb6d7bc

SCANNED = [
    "mcp-client-code",
    "tool-entity-manager",
    "tool-http",
    "tool-memory-delete",
    "tool-memory-save",
    "tool-memory-search",
    "tool-memory-update",
    "tool-project-manager",
    "tool-self-modify",
    "tool-task-manager",
    "tool-web-reader",
    "tool-web-search",
]
UNSUPPORTED = [
    ("mcp-builder-tool", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-agent-library", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-browser-use", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-expert-agent", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-library-manager", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-reminder-wf", "@n8n/n8n-nodes-langchain.toolWorkflow"),
    ("tool-telegram-status", "n8n-nodes-base.telegramTool"),
    ("tool-workflow-builder", "@n8n/n8n-nodes-langchain.toolWorkflow"),
]

# Plan fields each invalid example changes, and the error the check must return.
INVALID_PLAN_PATCHES: list[tuple[str, dict[str, JsonValue], str]] = [
    ("catalog-mismatch", {"catalog_id": "other-catalog"}, "STATIC_CATALOG_ID_MISMATCH"),
    ("unknown-template", {"template_version": "9.9.9"}, "STATIC_TEMPLATE_NOT_FOUND"),
    (
        "normal-input",
        {"normal_input": {"order_id": "ORD-1001"}},
        "STATIC_NORMAL_INPUT_NOT_ALLOWED",
    ),
    ("repeated-attempts", {"attempts_per_side": 2}, "STATIC_ATTEMPTS_INVALID"),
    ("timeout-over-template", {"timeout_seconds": 300}, "STATIC_TIMEOUT_EXCEEDED"),
    (
        "runtime-evidence-obligation",
        {
            "obligation_bindings": [
                {
                    "schema_version": "0.4.0",
                    "extensions": {},
                    "obligation_id": "owner-check-before-write",
                    "required_artifact_kinds": ["response", "final-state"],
                }
            ]
        },
        "STATIC_OBLIGATION_UNSUPPORTED",
    ),
]


def example_plan(catalog_id: str) -> TestPlan:
    return TestPlan(
        plan_id="plan-example-claw-first-fix-static",
        case_id="n8n-claw-memory-first-fix",
        analysis_id="analysis-example-claw-first-fix",
        authorship=EXAMPLE_AUTHOR,
        before_snapshot_id="snapshot-example-claw-4d1b1b7",
        after_snapshot_id="snapshot-example-claw-eb6d7bc",
        catalog_id=catalog_id,
        template_id="static-agent-tool-ownership",
        template_version="0.1.0",
        normal_input={},
        initial_state_ref=ArtifactRef(
            artifact_id="initial-state",
            path="input/state/initial.json",
            sha256="ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
            size_bytes=3,
            kind="source",
        ),
        obligation_bindings=[
            ObligationBinding(
                obligation_id="owner-check-before-write",
                required_artifact_kinds=["static-findings"],
            )
        ],
        uncovered_obligation_ids=["no-cross-user-memory-change"],
        attempts_per_side=1,
        timeout_seconds=120,
        planner_metadata={"security_outcome": None},
    )


def _finding(node: str, name: str, pointer: str, code_sha: str, line: int) -> StaticFinding:
    return StaticFinding(
        finding_id=f"finding-before-{node}",
        rule_id="flowdelta.n8n.agent-tool-write-without-ownership-check",
        rule_version="0.3.0",
        node_id=node,
        node_name=name,
        node_type="@n8n/n8n-nodes-langchain.toolCode",
        type_version="1.2",
        code_pointer=pointer,
        code_sha256=code_sha,
        line=line,
        severity="ERROR",
    )


def _run(
    repo: Path,
    side: Literal["before", "after"],
    snapshot_id: str,
    workflow_sha256: str,
    findings: list[StaticFinding],
    report_sha256: str,
    report_size: int,
) -> StaticScanRun:
    return StaticScanRun(
        run_id=f"static-run-example-{side}-1",
        comparison_run_id="comparison-example-claw-first-fix",
        case_id="n8n-claw-memory-first-fix",
        plan_id="plan-example-claw-first-fix-static",
        side=side,
        snapshot_id=snapshot_id,
        authorship=EXAMPLE_AUTHOR,
        template_id="static-agent-tool-ownership",
        template_version="0.1.0",
        tool=registered_catalog(repo).templates[0].tool,
        workflow_sha256=workflow_sha256,
        scanned_node_ids=SCANNED,
        unsupported=[
            UnsupportedTarget(node_id=node, node_type=kind, reason="TARGET_TYPE_NOT_SUPPORTED")
            for node, kind in UNSUPPORTED
        ],
        findings=findings,
        status="SUCCEEDED",
        raw_report=ArtifactRef(
            artifact_id=f"static-raw-report-{side}-1",
            path=f"runs/{side}-1/static-raw-report.json",
            sha256=report_sha256,
            size_bytes=report_size,
            kind="generated",
        ),
        started_at="2026-10-25T08:00:00Z",
        finished_at="2026-10-25T08:00:05Z",
        error_code=None,
    )


def example_runs(repo: Path) -> list[StaticScanRun]:
    before = _run(
        repo,
        "before",
        "snapshot-example-claw-4d1b1b7",
        BEFORE_SHA,
        [
            _finding(
                "tool-memory-delete",
                "Memory Delete",
                "/nodes/27/parameters/jsCode",
                "25cc2a33ef2cf5a6457e4d8ca932e9652fa41063d6d4dab7a3a3adb6386a99e0",
                7,
            ),
            _finding(
                "tool-memory-update",
                "Memory Update",
                "/nodes/28/parameters/jsCode",
                "292cab6084a79cf2045765717f30ef843586b07ed3224a5833e2f1b6ea5fa7db",
                51,
            ),
        ],
        "d67b6d58261757a685b4662573fde6e604678cd09859994cf083d7c1de425177",
        3986,
    )
    after = _run(
        repo,
        "after",
        "snapshot-example-claw-eb6d7bc",
        AFTER_SHA,
        [],
        "5ef57e32adab48cc42567a1bd0f211c84b14403b7368c8c479b5beef47715a7a",
        2158,
    )
    return [before, after]


def write_static_examples(target: Path, repo: Path) -> None:
    catalog = registered_catalog(repo)
    plan = example_plan(catalog.catalog_id)
    files: dict[str, object] = {
        "catalog.json": catalog.model_dump(mode="json"),
        "plan.json": plan.model_dump(mode="json"),
        "invalid-plans.json": [
            {"name": name, "patch": patch, "expected_error": code}
            for name, patch, code in INVALID_PLAN_PATCHES
        ],
    }
    for run in example_runs(repo):
        files[f"runs/{run.side}-1.static-run.json"] = run.model_dump(mode="json")
    for name, value in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json_bytes(value))
