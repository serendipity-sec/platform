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

WRAPPER_PREFIX = "async function __n8n_tool(query) {\n"
WRAPPER_SUFFIX = "\n}\n"
WRAPPER_LINE_OFFSET = 1

RUN_AUTHOR = Authorship(
    origin="RUNTIME_OBSERVATION",
    producer="flowdelta.runtime.static_adapter",
    note="등록 정적 검사 결과. 정적 조건이며 실제 정책 위반 효과가 아니다.",
)


class ToolTimeout(Exception):
    """도구가 plan의 시간 제한 안에 끝나지 않았다."""


ToolRunner = Callable[[list[str], int], tuple[int, bytes]]


def semgrep_runner(arguments: list[str], timeout: int) -> tuple[int, bytes]:
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
    workflow_bytes: bytes


@dataclass(frozen=True)
class StaticPreflight:
    template: StaticScanTemplate
    sides: tuple[StaticSide, StaticSide]
    timeout_seconds: int
    rule_bytes: bytes


def static_preflight(
    handoff: Handoff,
    plan: TestPlan,
    catalog: StaticScanCatalog,
    handoff_root: Path,
    repo: Path,
) -> StaticPreflight:
    case = handoff.case
    before, after = handoff.snapshots.snapshots
    if plan.case_id != case.case_id:
        raise InputError("STATIC_CASE_ID_MISMATCH")
    if (plan.before_snapshot_id, plan.after_snapshot_id) != (
        before.snapshot_id,
        after.snapshot_id,
    ):
        raise InputError("STATIC_SNAPSHOT_ID_MISMATCH")
    template = validate_static_plan(plan, catalog, repo)
    try:
        rule_bytes = (repo / template.tool.rule_path).read_bytes()
    except OSError as exc:
        raise InputError("STATIC_RULE_UNREADABLE") from exc
    if digest(rule_bytes) != template.tool.rule_sha256:
        raise InputError("STATIC_RULE_MISMATCH")

    required = {item.obligation_id: item for item in case.obligations}
    bound = {item.obligation_id for item in plan.obligation_bindings}
    if bound | set(plan.uncovered_obligation_ids) != set(required):
        raise InputError("STATIC_OBLIGATION_SET_MISMATCH")
    for binding in plan.obligation_bindings:
        expected = set(required[binding.obligation_id].required_artifacts)
        if not expected.issubset(template.result_kinds):
            raise InputError("STATIC_OBLIGATION_NOT_COVERABLE")
        # 계획이 주장한 증거가 case의 obligation 요구와 정확히 같아야 한다.
        if set(binding.required_artifact_kinds) != expected:
            raise InputError("STATIC_OBLIGATION_EVIDENCE_MISMATCH")

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
    return StaticPreflight(template, (sides[0], sides[1]), plan.timeout_seconds, rule_bytes)


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
    rule_copy: Path


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
        obligation_ids=[item.obligation_id for item in context.plan.obligation_bindings],
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


Severity = Literal["ERROR", "WARNING", "INFO"]
SEVERITIES: tuple[Severity, ...] = ("ERROR", "WARNING", "INFO")


@dataclass(frozen=True)
class _Hit:
    check_id: str
    path: str
    line: int
    severity: Severity


@dataclass(frozen=True)
class _Report:
    errors: list[object]
    hits: list[_Hit]
    scanned: list[str]


def _parse_report(stdout: bytes) -> _Report | None:
    try:
        report = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    errors = report.get("errors")
    results = report.get("results")
    paths = report.get("paths")
    if not isinstance(errors, list) or not isinstance(results, list):
        return None
    if not isinstance(paths, dict):
        return None
    scanned = paths.get("scanned")
    if not isinstance(scanned, list) or not all(isinstance(item, str) for item in scanned):
        return None
    hits = []
    for item in results:
        if not isinstance(item, dict):
            return None
        check_id, path = item.get("check_id"), item.get("path")
        start, extra = item.get("start"), item.get("extra")
        if not isinstance(check_id, str) or not isinstance(path, str):
            return None
        if not isinstance(start, dict) or not isinstance(extra, dict):
            return None
        line, severity = start.get("line"), extra.get("severity")
        if type(line) is not int or line < 1:
            return None
        if severity not in SEVERITIES:
            return None
        hits.append(_Hit(check_id, path, line, severity))
    return _Report(errors=errors, hits=hits, scanned=scanned)


def _rule_unchanged(context: _Context) -> bool:
    try:
        data = context.rule_copy.read_bytes()
    except OSError:
        return False
    return digest(data) == context.template.tool.rule_sha256


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

    # 실행 전후로 규칙 사본이 기록된 해시와 같은지 확인한다
    if not _rule_unchanged(context):
        return failed("FAILED", "STATIC_RULE_CHANGED")
    arguments = [
        "--metrics=off",
        "--disable-version-check",
        "--no-git-ignore",
        "--quiet",
        "--json",
        "--timeout",
        str(context.plan.timeout_seconds),
        "--config",
        str(context.rule_copy),
        str(code_dir),
    ]
    try:
        exit_code, stdout = context.runner(arguments, context.plan.timeout_seconds)
    except ToolTimeout:
        return failed("TIMED_OUT", "STATIC_TOOL_TIMEOUT")
    except FileNotFoundError:
        return failed("FAILED", "STATIC_TOOL_NOT_FOUND")
    except OSError:
        return failed("FAILED", "STATIC_TOOL_START_FAILED")
    finished = context.clock()
    if not _rule_unchanged(context):
        return failed("FAILED", "STATIC_RULE_CHANGED")

    raw_path = folder / "static-raw-report.json"
    raw_path.write_bytes(stdout)
    if exit_code not in (0, 1):
        return failed("FAILED", "STATIC_TOOL_FAILED")
    report = _parse_report(stdout)
    if report is None:
        return failed("FAILED", "STATIC_RAW_REPORT_INVALID")
    if report.errors:
        return failed("FAILED", "STATIC_TOOL_ERROR")
    read = {Path(item).resolve() for item in report.scanned}
    if set(by_file) - read:
        return failed("FAILED", "STATIC_SCAN_INCOMPLETE")

    findings = []
    for hit in report.hits:
        if not hit.check_id.endswith(template.tool.rule_id):
            return failed("FAILED", "STATIC_UNEXPECTED_RULE")
        source = by_file.get(Path(hit.path).resolve())
        if source is None:
            return failed("FAILED", "STATIC_RAW_REPORT_INVALID")
        line = hit.line - WRAPPER_LINE_OFFSET
        findings.append(
            StaticFinding(
                finding_id=f"finding-{side.side}-{digest(f'{source.node_id}:{line}'.encode())[:16]}",
                rule_id=template.tool.rule_id,
                rule_version=template.tool.rule_version,
                node_id=source.node_id,
                node_name=source.node_name,
                node_type=source.node_type,
                type_version=source.type_version,
                code_pointer=source.code_pointer,
                code_sha256=source.code_sha256,
                line=max(line, 1),
                severity=hit.severity,
            )
        )
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

    checked = static_preflight(handoff, plan, catalog, handoff_root, repo.resolve(strict=True))
    target = check_fresh_output(output, repo)
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise InputError("STATIC_OUTPUT_WRITE_FAILED") from exc
    # 저장소 규칙 파일이 실행 중 바뀌어도 검증한 바이트로만 검사한다
    rule_copy = target / "rule" / Path(checked.template.tool.rule_path).name
    try:
        rule_copy.parent.mkdir()
        rule_copy.write_bytes(checked.rule_bytes)
    except OSError as exc:
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
        rule_copy=rule_copy,
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
            raise InputError("STATIC_OUTPUT_WRITE_FAILED") from exc
        keep(run)
    if interrupted:
        raise KeyboardInterrupt
    return runs
