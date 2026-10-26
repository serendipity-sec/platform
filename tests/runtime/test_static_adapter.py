import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, digest, parse_file_model
from flowdelta.pipeline_contracts import ObligationBinding
from flowdelta.pipeline_contracts import TestPlan as Plan
from flowdelta.pipeline_io import read_handoff
from flowdelta.runtime import static_rules
from flowdelta.runtime.static_adapter import ToolTimeout, run_static_plan, semgrep_runner
from flowdelta.runtime.static_examples import example_plan
from flowdelta.runtime.static_scan import (
    SEMGREP_VERSION,
    StaticScanRun,
    registered_catalog,
    registered_templates,
)
from flowdelta.runtime.static_targets import find_targets

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "examples/contracts/order-lookup/input"
AGENT = "@n8n/n8n-nodes-langchain.agent"
TOOL_CODE = "@n8n/n8n-nodes-langchain.toolCode"

# Team-written synthetic tool bodies (not from any public workflow).
UNGUARDED = """const input = JSON.parse(query);
await helpers.httpRequest({ method: 'DELETE', url: `https://db.invalid/n?id=${input.id}` });
return 'deleted';"""
GUARDED = """const input = JSON.parse(query);
const rows = await helpers.httpRequest({ url: `https://db.invalid/n?id=${input.id}` });
const caller = $('Caller').first().json.userId;
if (rows[0].owner_user_id !== caller) return 'denied';
await helpers.httpRequest({ method: 'DELETE', url: `https://db.invalid/n?id=${input.id}` });
return 'deleted';"""


def node(
    node_id: str, name: str, kind: str, version: float, **parameters: object
) -> dict[str, object]:
    return {
        "id": node_id,
        "name": name,
        "type": kind,
        "typeVersion": version,
        "position": [0, 0],
        "parameters": parameters,
    }


def workflow(code: str | None, agent_version: float = 3.1) -> bytes:
    nodes = [
        node("agent", "Agent", AGENT, agent_version),
        node("memory-delete", "Memory Delete", TOOL_CODE, 1.2, jsCode=code),
        node("sub-flow", "Sub Flow", "@n8n/n8n-nodes-langchain.toolWorkflow", 2.2),
        node("helper", "Helper", TOOL_CODE, 1.2, jsCode=UNGUARDED),  # Not an agent tool.
    ]
    if code is None:
        nodes[1]["parameters"] = {}
    connections = {
        "Memory Delete": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
        "Sub Flow": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
        "Helper": {"main": [[{"node": "Agent", "type": "main", "index": 0}]]},
    }
    return json.dumps({"nodes": nodes, "connections": connections}).encode()


class Setup:
    """A repo copy with the rule, a handoff whose workflows are replaced, and a matching plan."""

    def __init__(self, root: Path, before: bytes, after: bytes) -> None:
        self.repo = root / "repo"
        rule = self.repo / static_rules.RULE_PATH
        rule.parent.mkdir(parents=True)
        shutil.copyfile(REPO / static_rules.RULE_PATH, rule)
        (self.repo / "var").mkdir()
        self.root = root / "handoff"
        shutil.copytree(REFERENCE, self.root)
        handoff = read_handoff(self.root)
        snapshots = []
        for snapshot, data in zip(handoff.snapshots.snapshots, (before, after), strict=True):
            (self.root / snapshot.workflow_ref.path).write_bytes(data)
            ref = snapshot.workflow_ref.model_copy(
                update={"sha256": digest(data), "size_bytes": len(data)}
            )
            snapshots.append(snapshot.model_copy(update={"workflow_ref": ref}))
        self.handoff: Handoff = handoff.model_copy(
            update={"snapshots": handoff.snapshots.model_copy(update={"snapshots": snapshots})}
        )
        self.catalog = registered_catalog(self.repo)
        first, second = snapshots
        self.plan = example_plan(self.catalog.catalog_id).model_copy(
            update={
                "case_id": handoff.case.case_id,
                "before_snapshot_id": first.snapshot_id,
                "after_snapshot_id": second.snapshot_id,
                "obligation_bindings": [],
                "uncovered_obligation_ids": [
                    item.obligation_id for item in handoff.case.obligations
                ],
            }
        )
        self.out = self.repo / "var" / "scan"

    def run(
        self, runner: Callable[[list[str], int], tuple[int, bytes]], **plan: object
    ) -> list[StaticScanRun]:
        checked = self.plan.model_copy(update=plan)
        return run_static_plan(
            self.handoff,
            checked,
            self.catalog,
            self.root,
            self.repo,
            self.out,
            runner,
            lambda: "2026-10-26T00:00:00Z",
        )


def fake_semgrep(
    flag: Callable[[str], bool] = lambda text: "owner_user_id" not in text,
    *,
    version: str = SEMGREP_VERSION,
    skip_read: bool = False,
    errors: list[object] | None = None,
    exit_code: int = 0,
    raise_on_scan: BaseException | None = None,
) -> Callable[[list[str], int], tuple[int, bytes]]:
    """Stands in for Semgrep: flags each target file whose text `flag` accepts, at line 3."""

    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        if arguments == ["--version"]:
            return 0, f"{version}\n".encode()
        if raise_on_scan is not None:
            raise raise_on_scan
        files = sorted(Path(arguments[-1]).glob("*.js"))
        results = [
            {
                "check_id": f"runtime.static-scan.rules.{static_rules.RULE_ID}",
                "path": str(path),
                "start": {"line": 3},
                "extra": {"severity": "ERROR"},
            }
            for path in files
            if flag(path.read_text(encoding="utf-8"))
        ]
        scanned = [] if skip_read else [str(path) for path in files]
        report = {"errors": errors or [], "results": results, "paths": {"scanned": scanned}}
        return exit_code, json.dumps(report).encode()

    return runner


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path, workflow(UNGUARDED), workflow(GUARDED))


def saved(setup: Setup, side: str) -> StaticScanRun:
    data = json.loads((setup.out / "runs" / f"{side}-1.static-run.json").read_bytes())
    return parse_file_model(StaticScanRun, data)


def test_targets_are_agent_tools_and_others_are_listed() -> None:
    template = registered_templates(REPO)[0]
    found = find_targets(workflow(UNGUARDED), template)
    assert [item.node_id for item in found.scanned] == ["memory-delete"]
    assert found.scanned[0].code_pointer == "/nodes/1/parameters/jsCode"
    assert found.scanned[0].code_sha256 == digest(UNGUARDED.encode())
    # The plain Code helper is not an agent tool; the sub-workflow tool is reported.
    assert [(item.node_id, item.reason) for item in found.unsupported] == [
        ("sub-flow", "TARGET_TYPE_NOT_SUPPORTED")
    ]
    missing = find_targets(workflow(None), template)
    assert ("memory-delete", "CODE_PARAMETER_MISSING") in [
        (item.node_id, item.reason) for item in missing.unsupported
    ]
    old_agent = find_targets(workflow(UNGUARDED, agent_version=1.7), template)
    assert old_agent.scanned == []
    assert {item.reason for item in old_agent.unsupported} == {"AGENT_VERSION_NOT_SUPPORTED"}
    with pytest.raises(InputError, match="STATIC_WORKFLOW_INVALID"):
        find_targets(b"[]", template)


def test_before_and_after_are_scanned_with_the_same_template(setup: Setup) -> None:
    before, after = setup.run(fake_semgrep())
    assert (before.status, after.status) == ("SUCCEEDED", "SUCCEEDED")
    assert before.comparison_run_id == after.comparison_run_id
    assert before.tool == after.tool == setup.catalog.templates[0].tool
    assert before.workflow_sha256 == digest(workflow(UNGUARDED))
    assert [(item.node_id, item.line, item.code_pointer) for item in before.findings] == [
        ("memory-delete", 2, "/nodes/1/parameters/jsCode")  # File line 3 minus the wrapper.
    ]
    assert after.findings == []
    assert before.scanned_node_ids == ["memory-delete"]
    assert [item.node_id for item in before.unsupported] == ["sub-flow"]
    assert before.outcome is None and before.authorship.origin == "RUNTIME_OBSERVATION"
    assert saved(setup, "before") == before and saved(setup, "after") == after
    assert before.raw_report is not None
    raw = setup.out / before.raw_report.path
    assert digest(raw.read_bytes()) == before.raw_report.sha256


def test_side_without_supported_target_is_not_run(tmp_path: Path) -> None:
    setup = Setup(tmp_path, workflow(UNGUARDED, agent_version=1.7), workflow(GUARDED))
    before, after = setup.run(fake_semgrep())
    assert (before.status, before.error_code, before.findings) == (
        "NOT_RUN",
        "NO_SUPPORTED_TARGET",
        [],
    )
    assert before.unsupported and after.status == "SUCCEEDED"


@pytest.mark.parametrize(
    ("runner", "status", "code"),
    [
        (fake_semgrep(version="1.0.0"), "FAILED", "STATIC_TOOL_VERSION_MISMATCH"),
        (fake_semgrep(raise_on_scan=FileNotFoundError()), "FAILED", "STATIC_TOOL_NOT_FOUND"),
        (fake_semgrep(raise_on_scan=ToolTimeout()), "TIMED_OUT", "STATIC_TOOL_TIMEOUT"),
        (fake_semgrep(exit_code=2), "FAILED", "STATIC_TOOL_FAILED"),
        (fake_semgrep(errors=[{"message": "parse"}]), "FAILED", "STATIC_TOOL_ERROR"),
        (fake_semgrep(skip_read=True), "FAILED", "STATIC_SCAN_INCOMPLETE"),
    ],
)
def test_tool_problems_never_look_like_no_finding(
    setup: Setup, runner: Callable[[list[str], int], tuple[int, bytes]], status: str, code: str
) -> None:
    runs = setup.run(runner)
    assert [(run.status, run.error_code, run.findings) for run in runs] == [(status, code, [])] * 2


def test_unreadable_report_fails(setup: Setup) -> None:
    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        return (
            (0, f"{SEMGREP_VERSION}\n".encode()) if arguments == ["--version"] else (0, b"not json")
        )

    assert {run.error_code for run in setup.run(runner)} == {"STATIC_RAW_REPORT_INVALID"}


def test_plan_timeout_is_given_to_the_tool(setup: Setup) -> None:
    seen: list[int] = []
    inner = fake_semgrep()

    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        if arguments != ["--version"]:
            seen.append(timeout)
            assert arguments[arguments.index("--timeout") + 1] == "45"
        return inner(arguments, timeout)

    setup.run(runner, timeout_seconds=45)
    assert seen == [45, 45]


def test_interrupt_is_recorded_for_both_sides(setup: Setup) -> None:
    with pytest.raises(KeyboardInterrupt):
        setup.run(fake_semgrep(raise_on_scan=KeyboardInterrupt()))
    assert (saved(setup, "before").status, saved(setup, "before").error_code) == (
        "INTERRUPTED",
        "STATIC_INTERRUPTED",
    )
    assert (saved(setup, "after").status, saved(setup, "after").error_code) == (
        "NOT_RUN",
        "PREVIOUS_SIDE_INTERRUPTED",
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"case_id": "other-case"}, "STATIC_CASE_ID_MISMATCH"),
        ({"before_snapshot_id": "snapshot-other"}, "STATIC_SNAPSHOT_ID_MISMATCH"),
        ({"uncovered_obligation_ids": []}, "STATIC_OBLIGATION_SET_MISMATCH"),
        ({"attempts_per_side": 2}, "STATIC_ATTEMPTS_INVALID"),
    ],
)
def test_bad_plan_is_refused_before_anything_is_written(
    setup: Setup, change: dict[str, object], code: str
) -> None:
    with pytest.raises(InputError, match=code):
        setup.run(fake_semgrep(), **change)
    assert not setup.out.exists()


def test_static_scan_cannot_claim_a_runtime_obligation(setup: Setup) -> None:
    # order-lookup obligations need responses and state, which a static scan does not produce.
    obligation = setup.handoff.case.obligations[0].obligation_id
    with pytest.raises(InputError, match="STATIC_OBLIGATION_NOT_COVERABLE"):
        setup.run(
            fake_semgrep(),
            obligation_bindings=[
                ObligationBinding(
                    obligation_id=obligation, required_artifact_kinds=["static-findings"]
                )
            ],
            uncovered_obligation_ids=[
                item.obligation_id
                for item in setup.handoff.case.obligations
                if item.obligation_id != obligation
            ],
        )
    assert not setup.out.exists()


def test_changed_workflow_bytes_and_output_outside_var_are_refused(setup: Setup) -> None:
    path = setup.root / setup.handoff.snapshots.snapshots[1].workflow_ref.path
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        setup.run(fake_semgrep())
    path.write_bytes(original)
    setup.out = setup.repo / "scan"
    with pytest.raises(InputError, match="OUTPUT_MUST_BE_UNDER_VAR"):
        setup.run(fake_semgrep())
    assert not setup.out.exists()


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep is not installed")
def test_registered_rule_through_the_adapter(setup: Setup) -> None:
    before, after = setup.run(semgrep_runner)
    assert (before.status, after.status) == ("SUCCEEDED", "SUCCEEDED")
    assert [item.node_id for item in before.findings] == ["memory-delete"]
    assert after.findings == []


def test_plan_type_is_the_shared_test_plan() -> None:
    # B hands over the shared TestPlan; the adapter adds no plan type of its own.
    assert Plan.__name__ == "TestPlan"


def test_file_replaced_after_the_check_is_not_scanned(setup: Setup) -> None:
    # #51 review P1: the bytes checked against the handoff are the bytes scanned.
    path = setup.root / setup.handoff.snapshots.snapshots[0].workflow_ref.path
    inner = fake_semgrep()

    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        if arguments == ["--version"]:
            path.write_bytes(workflow(GUARDED))  # Swap the file between check and scan.
        return inner(arguments, timeout)

    before, _ = setup.run(runner)
    assert before.status == "SUCCEEDED"
    assert before.workflow_sha256 == digest(workflow(UNGUARDED))
    assert [item.node_id for item in before.findings] == ["memory-delete"]
    assert [item.code_sha256 for item in before.findings] == [digest(UNGUARDED.encode())]
    scanned = (setup.out / "before" / "targets" / "001.js").read_text(encoding="utf-8")
    assert UNGUARDED in scanned and "owner_user_id" not in scanned


def test_tool_start_error_on_after_keeps_before(setup: Setup) -> None:
    # #51 review P2: a later tool error must not lose the side that already finished.
    inner = fake_semgrep()
    scans: list[str] = []

    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        if arguments != ["--version"]:
            scans.append(arguments[-1])
            if len(scans) == 2:
                raise PermissionError("denied")
        return inner(arguments, timeout)

    before, after = setup.run(runner)
    assert before.status == "SUCCEEDED" and before.findings
    assert (after.status, after.error_code, after.findings) == (
        "FAILED",
        "STATIC_TOOL_START_FAILED",
        [],
    )
    assert saved(setup, "before") == before and saved(setup, "after") == after


def test_version_check_problems_are_recorded_for_both_sides(setup: Setup) -> None:
    def denied(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        raise PermissionError("denied")

    runs = setup.run(denied)
    assert [(run.status, run.error_code) for run in runs] == [
        ("FAILED", "STATIC_TOOL_START_FAILED")
    ] * 2
    assert [saved(setup, side).status for side in ("before", "after")] == ["FAILED"] * 2


def test_interrupt_during_version_check_is_recorded(setup: Setup) -> None:
    def interrupted(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        setup.run(interrupted)
    for side in ("before", "after"):
        run = saved(setup, side)
        assert (run.status, run.error_code, run.findings) == (
            "INTERRUPTED",
            "STATIC_INTERRUPTED",
            [],
        )


def test_output_write_failure_is_not_a_tool_result(setup: Setup) -> None:
    inner = fake_semgrep()

    def runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
        if arguments != ["--version"]:
            # A plain file where the run folder must go: the output folder is broken.
            (Path(arguments[-1]).parents[1] / "runs").write_text("x", encoding="utf-8")
        return inner(arguments, timeout)

    with pytest.raises(InputError, match="STATIC_OUTPUT_WRITE_FAILED"):
        setup.run(runner)
