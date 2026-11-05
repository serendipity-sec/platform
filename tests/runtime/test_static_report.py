import json
import os
from pathlib import Path

import pytest

from flowdelta.ingestion import InputError, digest
from flowdelta.runtime.static_examples import example_runs
from flowdelta.runtime.static_report import SUMMARY_NAME, render_summary, summarize_folder
from flowdelta.runtime.static_scan import StaticScanRun

REPO = Path(__file__).resolve().parents[2]


def test_summary_states_it_is_not_a_verdict_and_names_the_warnings() -> None:
    before, after = example_runs(REPO)
    text = render_summary([before, after])
    assert "보안 판정이나 실행 효과가 아닙니다" in text
    assert "경고가 없다는 것은 안전하다는 뜻이 아닙니다" in text
    assert "tool-memory-delete" in text
    assert "## after 경고" in text
    assert text.count("Memory Delete") >= 4


def write_folder(root: Path, runs: list[StaticScanRun], raw: dict[str, bytes]) -> Path:
    folder = root / "out"
    (folder / "runs").mkdir(parents=True)
    for side, data in raw.items():
        (folder / side).mkdir()
        (folder / side / "static-raw-report.json").write_bytes(data)
    for run in runs:
        (folder / "runs" / f"{run.side}-1.static-run.json").write_text(
            json.dumps(run.model_dump(mode="json")), encoding="utf-8"
        )
    return folder


def with_raw(run: StaticScanRun, data: bytes) -> StaticScanRun:
    ref = run.raw_report
    assert ref is not None
    return run.model_copy(
        update={
            "raw_report": ref.model_copy(update={"sha256": digest(data), "size_bytes": len(data)})
        }
    )


def test_summary_is_written_next_to_runs_when_raw_reports_match(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    raw = {"before": b'{"results": []}', "after": b'{"results": [1]}'}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    target = summarize_folder(folder)
    assert target == folder / SUMMARY_NAME
    assert "# 정적 검사 요약" in target.read_text(encoding="utf-8")


def test_changed_raw_report_is_refused(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    raw = {"before": b"original", "after": b"original"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    (folder / "before" / "static-raw-report.json").write_bytes(b"edited")
    with pytest.raises(InputError, match="STATIC_RAW_REPORT_MISMATCH"):
        summarize_folder(folder)
    assert not (folder / SUMMARY_NAME).exists()


def test_runs_from_two_executions_in_one_folder_are_refused(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    other = after.model_copy(update={"comparison_run_id": "comparison-other"})
    folder = write_folder(tmp_path, [before, other], {})
    with pytest.raises(InputError, match="STATIC_REPORT_MIXED_RUNS"):
        summarize_folder(folder)


def test_folder_without_runs_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    (folder / "runs").mkdir(parents=True)
    with pytest.raises(InputError, match="STATIC_REPORT_NO_RUNS"):
        summarize_folder(folder)


def test_existing_summary_is_kept_and_not_overwritten(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    raw = {"before": b"raw-b", "after": b"raw-a"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    first = summarize_folder(folder).read_text(encoding="utf-8")
    (folder / SUMMARY_NAME).write_text("기록된 요약", encoding="utf-8")
    with pytest.raises(InputError, match="STATIC_SUMMARY_EXISTS"):
        summarize_folder(folder)
    assert (folder / SUMMARY_NAME).read_text(encoding="utf-8") == "기록된 요약"
    assert first.startswith("# 정적 검사 요약")


def test_summary_is_not_written_through_a_link_to_another_file(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    raw = {"before": b"raw-b", "after": b"raw-a"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    outside = tmp_path / "outside.txt"
    outside.write_text("외부 파일", encoding="utf-8")
    try:
        (folder / SUMMARY_NAME).hardlink_to(outside)
    except OSError:
        pytest.skip("hard links are not available on this file system")
    with pytest.raises(InputError, match="STATIC_SUMMARY_EXISTS"):
        summarize_folder(folder)
    assert outside.read_text(encoding="utf-8") == "외부 파일"


def test_external_names_cannot_break_the_table_or_inject_markdown(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    hostile = "Memory\n\n## INJECTED | x ![pixel](https://example.invalid/p.png)"
    node = before.findings[0].model_copy(update={"node_name": hostile})
    crafted = before.model_copy(update={"findings": [node]})
    text = render_summary([crafted, after])
    assert "\n## INJECTED" not in text
    assert "\n![pixel]" not in text
    assert "\#\# INJECTED" in text
    assert "\|" in text
    row = next(line for line in text.splitlines() if "Memory" in line and "tool-memory" in line)
    assert row.startswith("| ") and row.endswith(" |")


def test_folder_with_only_a_before_result_is_refused(tmp_path: Path) -> None:
    before, _ = example_runs(REPO)
    raw = {"before": b"raw-b"}
    folder = write_folder(tmp_path, [with_raw(before, raw["before"])], raw)
    with pytest.raises(InputError, match="STATIC_REPORT_SIDES_INCOMPLETE"):
        summarize_folder(folder)
    assert not (folder / SUMMARY_NAME).exists()


def test_folder_with_two_after_results_is_refused(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    second_after = after.model_copy(update={"run_id": after.run_id + "-2"})
    raw = {"before": b"raw-b", "after": b"raw-a"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"]), second_after]
    folder = write_folder(tmp_path, runs, raw)
    (folder / "runs" / "after-2.static-run.json").write_text(
        json.dumps(second_after.model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(InputError, match="STATIC_REPORT_SIDES_INCOMPLETE"):
        summarize_folder(folder)


def test_oversized_raw_report_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flowdelta.runtime import static_report

    before, after = example_runs(REPO)
    raw = {"before": b"0123456789" * 4, "after": b"raw-a"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    monkeypatch.setattr(static_report, "RAW_REPORT_MAX_BYTES", 8)
    with pytest.raises(InputError, match="STATIC_RAW_REPORT_TOO_LARGE"):
        summarize_folder(folder)


def test_raw_report_over_the_real_limit_is_refused_as_too_large(tmp_path: Path) -> None:
    from flowdelta.runtime.static_report import RAW_REPORT_MAX_BYTES

    before, after = example_runs(REPO)
    big = b"x" * (RAW_REPORT_MAX_BYTES + 1)
    raw = {"before": big, "after": b"raw-a"}
    recorded = with_raw(before, b"small")  # 기록 크기는 작지만 실제 파일은 상한을 넘는다
    folder = write_folder(tmp_path, [recorded, with_raw(after, raw["after"])], raw)
    with pytest.raises(InputError, match="STATIC_RAW_REPORT_TOO_LARGE"):
        summarize_folder(folder)


def test_raw_report_that_grows_during_the_check_is_stopped_at_the_recorded_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flowdelta.runtime import static_report

    before, after = example_runs(REPO)
    raw = {"before": b"0123456789", "after": b"raw-a"}
    runs = [with_raw(before, raw["before"]), with_raw(after, raw["after"])]
    folder = write_folder(tmp_path, runs, raw)
    # 크기 확인 때는 기록 크기처럼 보이게 하고, 실제로는 읽는 동안 파일이 커진 상황을 흉내 낸다.
    real_fstat = os.fstat
    (folder / "before" / "static-raw-report.json").write_bytes(b"0123456789" + b"EXTRA")

    def fake_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result(
            (
                result.st_mode,
                result.st_ino,
                result.st_dev,
                result.st_nlink,
                result.st_uid,
                result.st_gid,
                10,
                result.st_atime,
                result.st_mtime,
                result.st_ctime,
            )
        )

    monkeypatch.setattr(static_report.os, "fstat", fake_fstat)
    with pytest.raises(InputError, match="STATIC_RAW_REPORT_MISMATCH"):
        summarize_folder(folder)


def test_tool_metadata_and_code_pointer_cannot_inject_markdown(tmp_path: Path) -> None:
    before, after = example_runs(REPO)
    hostile_tool = before.tool.model_copy(update={"version": "1.0\n\n## TOOL-INJECT"})
    finding = before.findings[0].model_copy(update={"code_pointer": "/x`\n## POINTER-INJECT"})
    crafted = before.model_copy(update={"tool": hostile_tool, "findings": [finding]})
    text = render_summary([crafted, after])
    assert "\n## TOOL-INJECT" not in text
    assert "\n## POINTER-INJECT" not in text
    assert "TOOL-INJECT" in text
