"""B와 리뷰어가 읽을 정적 검사 예시 파일(`examples/static-scan/`)을 만든다.

catalog는 등록된 template에서 생성한다. plan, 잘못된 plan, run은 n8n-claw 첫 수정 쌍
(4d1b1b7 -> eb6d7bc)을 위해 손으로 쓴 계약 예시다. finding 값은 `experiments/n8n-claw-c`의
도구 적합성 실행 결과다. 원시 보고서에는 원본 코드가 있어 `var/`에만 두고 해시와 크기만 적는다.
"""

from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import ArtifactRef
from flowdelta.ingestion import json_bytes
from flowdelta.pipeline_contracts import Authorship, ObligationBinding, TestPlan
from flowdelta.runtime import static_rules
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

# 고정한 n8n-claw 원본 해시 (출처: experiments/n8n-claw-c/sources.json).
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

# 잘못된 plan 예시가 바꾸는 필드와 기대 오류 코드.
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
        rule_version=static_rules.RULE_VERSION,
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
        "d3f3e59d7f3964ae736440fb8aa3159bb1b54e60a4a0b80ac622441c0bfd6acd",
        5889,
    )
    after = _run(
        repo,
        "after",
        "snapshot-example-claw-eb6d7bc",
        AFTER_SHA,
        [],
        "c399b1391ad019a80fb3dd23476e4fc7d878854fbc7b3048c5e18eaa57ca0012",
        3229,
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
