"""Run a registered static template for a plan, before and after (#51).

The adapter takes the plan, the C static catalog and the handoff A and B used. It checks all
of them before reading any workflow code, extracts the code each side's template reads, runs
the pinned tool once per side with the plan's time limit, and writes one `StaticScanRun` per
side. Nothing from the target is executed and no request is sent.

What is recorded instead of hidden:
- a side with no supported target is `NOT_RUN` with `NO_SUPPORTED_TARGET`, not "no finding";
- a tool error, an unreadable report or a target the tool did not read is `FAILED`;
- the time limit is `TIMED_OUT`, Ctrl+C is `INTERRUPTED` (a side not started is `NOT_RUN`);
- each side's run file is written as soon as that side ends.
Only `SUCCEEDED` runs carry findings. Raw reports contain original code and stay in the
output folder under `var/`.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from flowdelta.contracts import ArtifactRef, Handoff
from flowdelta.ingestion import InputError, _read_local, digest, json_bytes
from flowdelta.pipeline_contracts import Authorship, TestPlan
from flowdelta.runtime.preflight import check_fresh_output
from flowdelta.runtime.static_scan import (
    StaticFinding,
    StaticScanCatalog,
    StaticScanRun,
    StaticScanStatus,
    StaticScanTemplate,
    UnsupportedTarget,
    validate_static_plan,
)
from flowdelta.runtime.static_targets import CodeTarget, find_targets

# One line is added before the code so the tool parses a tool body (`return` at top level).
WRAPPER_PREFIX = "async function __n8n_tool(query) {\n"
WRAPPER_SUFFIX = "\n}\n"
WRAPPER_LINE_OFFSET = 1

RUN_AUTHOR = Authorship(
    origin="RUNTIME_OBSERVATION",
    producer="flowdelta.runtime.static_adapter",
    note="등록 정적 검사 결과. 정적 조건이며 실제 정책 위반 효과가 아니다.",
)


class ToolTimeout(Exception):
    """The tool did not finish within the plan's time limit."""


# (arguments, timeout seconds) -> (exit code, stdout). Raises FileNotFoundError or ToolTimeout.
ToolRunner = Callable[[list[str], int], tuple[int, bytes]]


def semgrep_runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
    # No version check or metrics: both are network calls, and the scan must work offline.
    env = {**os.environ, "SEMGREP_ENABLE_VERSION_CHECK": "0", "SEMGREP_SEND_METRICS": "off"}
    try:
        done = subprocess.run(
            ["semgrep", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout from exc
    return done.returncode, done.stdout


@dataclass(frozen=True)
class StaticSide:
    side: Literal["before", "after"]
    snapshot_id: str
    workflow_path: Path
    workflow_sha256: str
    # The bytes checked against the handoff. They are scanned as-is; the file is not read
    # again, so a file replaced after the check cannot be scanned under the recorded hash.
    workflow_bytes: bytes


@dataclass(frozen=True)
class StaticPreflight:
    template: StaticScanTemplate
    sides: tuple[StaticSide, StaticSide]
    timeout_seconds: int


def static_preflight(
    handoff: Handoff,
    plan: TestPlan,
    catalog: StaticScanCatalog,
    handoff_root: Path,
    repo: Path,
) -> StaticPreflight:
    """Every check that can fail without reading workflow code. Nothing is written here."""
    case = handoff.case
    before, after = handoff.snapshots.snapshots  # read_handoff guarantees before, after.
    if plan.case_id != case.case_id:
        raise InputError("STATIC_CASE_ID_MISMATCH")
    if (plan.before_snapshot_id, plan.after_snapshot_id) != (
        before.snapshot_id,
        after.snapshot_id,
    ):
        raise InputError("STATIC_SNAPSHOT_ID_MISMATCH")
    template = validate_static_plan(plan, catalog, repo)

    # Every case obligation is either bound or explicitly uncovered. A static scan can only
    # bind an obligation whose required evidence it actually produces; obligations that need
    # runtime state (for example final-state) stay uncovered instead of being claimed.
    required = {item.obligation_id: item for item in case.obligations}
    bound = {item.obligation_id for item in plan.obligation_bindings}
    if bound | set(plan.uncovered_obligation_ids) != set(required):
        raise InputError("STATIC_OBLIGATION_SET_MISMATCH")
    for obligation_id in bound:
        if not set(required[obligation_id].required_artifacts).issubset(template.result_kinds):
            raise InputError("STATIC_OBLIGATION_NOT_COVERABLE")

    # Read each workflow once and keep the bytes that match the handoff (same hash and size).
    root = handoff_root.resolve(strict=True)
    sides = []
    for snapshot in (before, after):
        ref = snapshot.workflow_ref
        data = _read_local(root, ref.path)
        if len(data) != ref.size_bytes or digest(data) != ref.sha256:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        sides.append(
            StaticSide(
                side=snapshot.side,
                snapshot_id=snapshot.snapshot_id,
                workflow_path=root / ref.path,
                workflow_sha256=ref.sha256,
                workflow_bytes=data,
            )
        )
    return StaticPreflight(template, (sides[0], sides[1]), plan.timeout_seconds)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class _Context:
    plan: TestPlan
    case_id: str
    comparison_run_id: str
    template: StaticScanTemplate
    repo: Path
    output: Path
    runner: ToolRunner
    clock: Callable[[], str]


def _record(
    context: _Context,
    side: StaticSide,
    status: StaticScanStatus,
    *,
    error_code: str | None = None,
    scanned: list[CodeTarget] | None = None,
    unsupported: list[UnsupportedTarget] | None = None,
    findings: list[StaticFinding] | None = None,
    raw_report: ArtifactRef | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> StaticScanRun:
    return StaticScanRun(
        run_id=f"static-run-{uuid4().hex}",
        comparison_run_id=context.comparison_run_id,
        case_id=context.case_id,
        plan_id=context.plan.plan_id,
        side=side.side,
        snapshot_id=side.snapshot_id,
        authorship=RUN_AUTHOR,
        template_id=context.template.template_id,
        template_version=context.template.version,
        tool=context.template.tool,
        workflow_sha256=side.workflow_sha256,
        scanned_node_ids=[item.node_id for item in scanned or []],
        unsupported=unsupported or [],
        findings=findings or [],
        status=status,
        raw_report=raw_report,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
    )


def _tool_version_error(context: _Context) -> str | None:
    try:
        code, out = context.runner(["--version"], 60)
    except FileNotFoundError:
        return "STATIC_TOOL_NOT_FOUND"
    except ToolTimeout:
        return "STATIC_TOOL_VERSION_TIMEOUT"
    except OSError:
        return "STATIC_TOOL_START_FAILED"
    if code != 0 or out.decode("utf-8", "replace").strip() != context.template.tool.version:
        return "STATIC_TOOL_VERSION_MISMATCH"
    return None


def _scan_side(context: _Context, side: StaticSide) -> StaticScanRun:
    template = context.template
    targets = find_targets(side.workflow_bytes, template)
    if not targets.scanned:
        return _record(
            context,
            side,
            "NOT_RUN",
            error_code="NO_SUPPORTED_TARGET",
            unsupported=targets.unsupported,
        )

    folder = context.output / side.side
    code_dir = folder / "targets"
    code_dir.mkdir(parents=True)
    by_file: dict[Path, CodeTarget] = {}
    for number, target in enumerate(targets.scanned, start=1):
        path = code_dir / f"{number:03d}.js"
        path.write_text(
            WRAPPER_PREFIX + target.code + WRAPPER_SUFFIX, encoding="utf-8", newline="\n"
        )
        by_file[path.resolve()] = target
    (folder / "targets.json").write_bytes(
        json_bytes(
            [
                {
                    "file": f"targets/{path.name}",
                    "node_id": item.node_id,
                    "code_pointer": item.code_pointer,
                    "code_sha256": item.code_sha256,
                    "wrapper_line_offset": WRAPPER_LINE_OFFSET,
                }
                for path, item in by_file.items()
            ]
        )
    )

    started = context.clock()

    def failed(status: StaticScanStatus, code: str) -> StaticScanRun:
        return _record(
            context,
            side,
            status,
            error_code=code,
            scanned=targets.scanned,
            unsupported=targets.unsupported,
            started_at=started,
            finished_at=context.clock(),
        )

    rule = context.repo / template.tool.rule_path
    arguments = [
        "--metrics=off",
        "--disable-version-check",
        # Inside a git checkout Semgrep reads only tracked files; var/ is untracked.
        "--no-git-ignore",
        "--quiet",
        "--json",
        "--timeout",
        str(context.plan.timeout_seconds),
        "--config",
        str(rule),
        str(code_dir),
    ]
    try:
        exit_code, stdout = context.runner(arguments, context.plan.timeout_seconds)
    except ToolTimeout:
        return failed("TIMED_OUT", "STATIC_TOOL_TIMEOUT")
    except FileNotFoundError:
        return failed("FAILED", "STATIC_TOOL_NOT_FOUND")
    except OSError:
        # The tool could not be started (for example permission denied). The output folder
        # is fine, so this is a tool failure, not an output write failure.
        return failed("FAILED", "STATIC_TOOL_START_FAILED")
    finished = context.clock()

    raw_path = folder / "static-raw-report.json"
    raw_path.write_bytes(stdout)
    if exit_code not in (0, 1):
        return failed("FAILED", "STATIC_TOOL_FAILED")
    try:
        report = json.loads(stdout)
        errors = report["errors"]
        results = report["results"]
        read = {Path(item).resolve() for item in report["paths"]["scanned"]}
    except (ValueError, KeyError, TypeError):
        return failed("FAILED", "STATIC_RAW_REPORT_INVALID")
    if errors:
        return failed("FAILED", "STATIC_TOOL_ERROR")
    if set(by_file) - read:
        # A file the tool did not read cannot count as "no finding".
        return failed("FAILED", "STATIC_SCAN_INCOMPLETE")

    findings = []
    try:
        for hit in results:
            if not str(hit["check_id"]).endswith(template.tool.rule_id):
                return failed("FAILED", "STATIC_UNEXPECTED_RULE")
            target = by_file[Path(hit["path"]).resolve()]
            line = int(hit["start"]["line"]) - WRAPPER_LINE_OFFSET
            severity = str(hit["extra"]["severity"])
            findings.append(
                StaticFinding(
                    finding_id=f"finding-{side.side}-{digest(f'{target.node_id}:{line}'.encode())[:16]}",
                    rule_id=template.tool.rule_id,
                    rule_version=template.tool.rule_version,
                    node_id=target.node_id,
                    node_name=target.node_name,
                    node_type=target.node_type,
                    type_version=target.type_version,
                    code_pointer=target.code_pointer,
                    code_sha256=target.code_sha256,
                    line=max(line, 1),
                    severity=severity,  # type: ignore[arg-type]
                )
            )
    except (KeyError, TypeError, ValueError):
        return failed("FAILED", "STATIC_RAW_REPORT_INVALID")
    unique = {item.finding_id: item for item in findings}

    data = raw_path.read_bytes()
    raw_report = ArtifactRef(
        artifact_id=f"static-raw-report-{side.side}-1",
        path=raw_path.relative_to(context.output).as_posix(),
        sha256=digest(data),
        size_bytes=len(data),
        kind="generated",
    )
    return _record(
        context,
        side,
        "SUCCEEDED",
        scanned=targets.scanned,
        unsupported=targets.unsupported,
        findings=sorted(unique.values(), key=lambda item: (item.node_id, item.line)),
        raw_report=raw_report,
        started_at=started,
        finished_at=finished,
    )


def run_static_plan(
    handoff: Handoff,
    plan: TestPlan,
    catalog: StaticScanCatalog,
    handoff_root: Path,
    repo: Path,
    output: Path,
    runner: ToolRunner = semgrep_runner,
    clock: Callable[[], str] = _now,
) -> list[StaticScanRun]:
    """Scan before and after with the same template and write `runs/<side>-1.static-run.json`.

    Raises InputError before anything is written when the inputs disagree. After that, each
    side's run file is written as soon as that side ends, so a later error or Ctrl+C never
    loses an earlier result. Ctrl+C during the tool version check marks both sides
    INTERRUPTED; during a side it marks that side INTERRUPTED and the rest NOT_RUN, and
    KeyboardInterrupt is re-raised after the files are written. A failure to write into the
    output folder itself is STATIC_OUTPUT_WRITE_FAILED (InputError), not a tool result.
    """
    checked = static_preflight(handoff, plan, catalog, handoff_root, repo.resolve(strict=True))
    target = check_fresh_output(output, repo)
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        # The output folder cannot be created (a file in the way, no permission, full disk).
        # Nothing was scanned, so this is an input error and not a tool result.
        raise InputError("STATIC_OUTPUT_WRITE_FAILED") from exc
    context = _Context(
        plan=plan,
        case_id=handoff.case.case_id,
        comparison_run_id=f"comparison-{uuid4().hex}",
        template=checked.template,
        repo=repo.resolve(),
        output=target,
        runner=runner,
        clock=clock,
    )
    runs: list[StaticScanRun] = []

    def keep(run: StaticScanRun) -> None:
        path = target / "runs" / f"{run.side}-1.static-run.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(json_bytes(run.model_dump(mode="json")))
        except OSError as exc:
            raise InputError("STATIC_OUTPUT_WRITE_FAILED") from exc
        runs.append(run)

    try:
        version_error = _tool_version_error(context)
    except KeyboardInterrupt:
        for side in checked.sides:
            keep(_record(context, side, "INTERRUPTED", error_code="STATIC_INTERRUPTED"))
        raise
    interrupted = False
    for side in checked.sides:
        if interrupted:
            keep(_record(context, side, "NOT_RUN", error_code="PREVIOUS_SIDE_INTERRUPTED"))
            continue
        if version_error is not None:
            keep(_record(context, side, "FAILED", error_code=version_error))
            continue
        try:
            run = _scan_side(context, side)
        except KeyboardInterrupt:
            interrupted = True
            run = _record(context, side, "INTERRUPTED", error_code="STATIC_INTERRUPTED")
        except OSError as exc:
            # Writing extracted code or the raw report into the output folder failed.
            raise InputError("STATIC_OUTPUT_WRITE_FAILED") from exc
        keep(run)
    if interrupted:
        raise KeyboardInterrupt
    return runs
