"""정적 검사 결과 폴더를 사람이 읽는 SUMMARY.md로 정리한다.

요약은 결과 JSON에서 파생되는 보기일 뿐이라 DB에는 저장하지 않는다. 원시 보고서에는
검사 대상 코드가 들어 있으므로 요약에는 코드 문자열을 넣지 않고 해시만 적는다.
"""

import hashlib
import os
from pathlib import Path
from typing import Annotated

import typer

from flowdelta.contracts import ArtifactRef
from flowdelta.ingestion import MAX_FILE_BYTES, InputError, json_bytes, parse_file_model, parse_json
from flowdelta.runtime.static_scan import StaticScanRun

SUMMARY_NAME = "SUMMARY.md"
# 원시 보고서는 검사 출력 전체라서 결과 JSON과 따로 제한한다.
RAW_REPORT_MAX_BYTES = MAX_FILE_BYTES
NOTICE = (
    "> 이 요약은 정적 조건 결과입니다. 실제 보안 판정이나 실행 효과가 아닙니다. "
    "경고가 없다는 것은 안전하다는 뜻이 아닙니다."
)


def read_runs(folder: Path) -> list[StaticScanRun]:
    """runs/*.static-run.json만 읽는다. 한 폴더는 한 실행 묶음이어야 한다."""
    files = sorted((folder / "runs").glob("*.static-run.json"))
    if not files:
        raise InputError("STATIC_REPORT_NO_RUNS")
    runs = []
    for path in files:
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise InputError("STATIC_RUN_READ_FAILED") from None
        if len(data) > MAX_FILE_BYTES:
            raise InputError("FILE_SIZE_LIMIT")
        runs.append(parse_file_model(StaticScanRun, parse_json(data)))
    if len({(run.comparison_run_id, run.plan_id) for run in runs}) != 1:
        # 재실행 결과가 섞여 있으면 어떤 결과를 요약할지 정할 수 없다.
        raise InputError("STATIC_REPORT_MIXED_RUNS")
    if sorted(run.side for run in runs) != ["after", "before"]:
        # before와 after가 정확히 한 개씩 있어야 비교 요약이 된다.
        raise InputError("STATIC_REPORT_SIDES_INCOMPLETE")
    return sorted(runs, key=lambda run: run.side)


def _check_raw_report(path: Path, expected: ArtifactRef) -> None:
    """같은 파일 핸들로 크기를 확인하고, 기록된 크기까지만 읽어 해시를 계산한다."""
    try:
        stream = path.open("rb")
    except OSError:
        raise InputError("STATIC_RAW_REPORT_MISSING") from None
    with stream:
        try:
            size = os.fstat(stream.fileno()).st_size
        except OSError:
            raise InputError("STATIC_RAW_REPORT_MISSING") from None
        # 크기 상한을 먼저 본다. 기록 크기가 상한 안이어도 실제 파일이 크면 여기서 거부된다.
        if size > RAW_REPORT_MAX_BYTES:
            raise InputError("STATIC_RAW_REPORT_TOO_LARGE")
        if size != expected.size_bytes:
            raise InputError("STATIC_RAW_REPORT_MISMATCH")
        digester = hashlib.sha256()
        read = 0
        while True:
            # 읽는 중에 파일이 커져도 기록 크기를 1바이트라도 넘으면 즉시 중단한다.
            chunk = stream.read(min(1 << 20, expected.size_bytes + 1 - read))
            if not chunk:
                break
            read += len(chunk)
            if read > expected.size_bytes:
                raise InputError("STATIC_RAW_REPORT_MISMATCH")
            digester.update(chunk)
    if digester.hexdigest() != expected.sha256:
        raise InputError("STATIC_RAW_REPORT_MISMATCH")


def check_raw_reports(folder: Path, runs: list[StaticScanRun]) -> None:
    """성공한 실행의 원시 보고서가 기록된 경로·크기·해시와 맞는지 확인한다."""
    root = folder.resolve()
    for run in runs:
        if run.raw_report is None:
            continue
        path = (folder / run.raw_report.path).resolve()
        if not path.is_relative_to(root):
            raise InputError("STATIC_RAW_REPORT_OUTSIDE_FOLDER")
        _check_raw_report(path, run.raw_report)


_MARKDOWN_SPECIAL = set("\\`*_[]()#<>|!~")


def _text(value: object) -> str:
    """검사 대상 workflow에서 온 문자열을 Markdown 글자로만 보이게 한다."""
    flat = " ".join(str(value).splitlines())
    return "".join("\\" + char if char in _MARKDOWN_SPECIAL else char for char in flat)


def _row(*cells: object) -> str:
    cells_text = [_text(cell) if isinstance(cell, str) else str(cell) for cell in cells]
    return "| " + " | ".join(cells_text) + " |"


def render_summary(runs: list[StaticScanRun]) -> str:
    first = runs[0]
    lines = [
        "# 정적 검사 요약",
        "",
        NOTICE,
        "",
        f"- 계획 ID: {_text(first.plan_id)}",
        f"- 사례 ID: {_text(first.case_id)}",
        f"- 실행 묶음: {_text(first.comparison_run_id)}",
        f"- 도구: {_text(first.tool.name)} {_text(first.tool.version)}, "
        f"규칙 {_text(first.tool.rule_id)} {_text(first.tool.rule_version)} "
        f"(`{first.tool.rule_sha256[:12]}`)",
        "",
        "## 실행 상태",
        "",
        "| 쪽 | 상태 | 오류 코드 | 검사한 노드 | 경고 | 미지원 | 원시 보고서 해시 |",
        "|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        raw = run.raw_report.sha256[:12] if run.raw_report else "-"
        lines.append(
            _row(
                run.side,
                run.status,
                run.error_code or "-",
                len(run.scanned_node_ids),
                len(run.findings),
                len(run.unsupported),
                raw,
            )
        )
    for run in runs:
        lines += ["", f"## {_text(run.side)} 경고", ""]
        if run.status != "SUCCEEDED":
            lines.append(
                f"검사가 `{run.status}`로 끝났습니다. 경고 목록이 비어 있어도 "
                "안전하다는 뜻이 아닙니다."
            )
        elif not run.findings:
            lines.append("지원하는 검사 범위에서 경고가 없었습니다. 이는 안전의 증명이 아닙니다.")
        else:
            lines += [
                "| 노드 | 종류 | 줄 | 코드 위치 | 코드 해시 |",
                "|---|---|---|---|---|",
            ]
            for item in sorted(run.findings, key=lambda f: (f.node_name, f.line)):
                # 문자열은 _row가 이스케이프한다. 여기서 역따옴표로 감싸면 표시가 깨진다.
                lines.append(
                    _row(
                        f"{item.node_name} ({item.node_id})",
                        item.node_type,
                        item.line,
                        item.code_pointer,
                        item.code_sha256[:12],
                    )
                )
        if run.unsupported:
            lines += ["", "미지원 대상 (검사하지 않음):", ""]
            for skipped in sorted(run.unsupported, key=lambda u: u.node_id):
                node = _text(skipped.node_id)
                kind = _text(skipped.node_type)
                lines.append(f"- {node} ({kind}): {_text(skipped.reason)}")
    return "\n".join(lines) + "\n"


def summarize_folder(folder: Path) -> Path:
    runs = read_runs(folder)
    check_raw_reports(folder, runs)
    target = folder / SUMMARY_NAME
    # 한 번 만든 요약은 기록이므로 덮어쓰지 않는다. 배타적 생성은 기존 파일과 링크도 거부한다.
    try:
        with target.open("x", encoding="utf-8", newline="\n") as file:
            file.write(render_summary(runs))
    except FileExistsError:
        raise InputError("STATIC_SUMMARY_EXISTS") from None
    return target


def summarize_command(
    folder: Annotated[Path, typer.Argument(help="Output folder that contains runs/.")],
) -> None:
    """Write SUMMARY.md next to runs/ for one static scan execution group."""
    try:
        target = summarize_folder(folder)
    except InputError as exc:
        typer.echo(json_bytes({"error": exc.code}).decode("utf-8"), err=True, nl=False)
        raise typer.Exit(2) from None
    typer.echo(
        json_bytes({"status": "STATIC_SUMMARY_WRITTEN", "path": target.as_posix()}).decode("utf-8"),
        nl=False,
    )
