import json
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
