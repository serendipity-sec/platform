import json
import os
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.ingestion import digest
from flowdelta.runtime.static_examples import example_runs

REPO = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    not DATABASE_URL, reason="set FLOWDELTA_KB_TEST_DATABASE_URL for PostgreSQL tests"
)


def test_cli_reports_missing_database_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLOWDELTA_KB_DATABASE_URL", raising=False)
    run_file = REPO / "examples/static-scan/runs/before-1.static-run.json"
    result = CliRunner().invoke(app, ["static-db", "store", str(run_file)])
    assert result.exit_code == 2
    assert "KNOWLEDGE_DATABASE_URL_REQUIRED" in result.output


@needs_database
def test_store_get_and_list_round_trip_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = os.environ.get("FLOWDELTA_KB_TEST_MIGRATION_URL", DATABASE_URL)
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("FLOWDELTA_KB_MIGRATION_URL", migration)
    runner = CliRunner()
    assert runner.invoke(app, ["static-db", "migrate"]).exit_code == 0

    before, _ = example_runs(REPO)
    token = uuid.uuid4().hex[:12]
    run = before.model_copy(
        update={"run_id": f"static-cli-{token}", "plan_id": f"plan-cli-{token}"}
    )
    # 실제 출력 폴더와 같은 구조로 만든다: runs/ 아래 결과 파일, 쪽 폴더 아래 원시 보고서.
    raw = b'{"results": []}'
    (tmp_path / "before").mkdir()
    (tmp_path / "before" / "static-raw-report.json").write_bytes(raw)
    (tmp_path / "runs").mkdir()
    ref = run.raw_report
    assert ref is not None
    run = run.model_copy(
        update={
            "raw_report": ref.model_copy(update={"sha256": digest(raw), "size_bytes": len(raw)})
        }
    )
    run_file = tmp_path / "runs" / "before-1.static-run.json"
    run_file.write_text(json.dumps(run.model_dump(mode="json")), encoding="utf-8")

    assert runner.invoke(app, ["static-db", "store", str(run_file)]).exit_code == 0

    got = tmp_path / "got.json"
    assert runner.invoke(app, ["static-db", "get", run.run_id, "--out", str(got)]).exit_code == 0
    assert json.loads(got.read_text(encoding="utf-8")) == run.model_dump(mode="json")

    listed = tmp_path / "listed.json"
    result = runner.invoke(app, ["static-db", "list", run.plan_id, "--out", str(listed)])
    assert result.exit_code == 0
    assert json.loads(listed.read_text(encoding="utf-8")) == [run.model_dump(mode="json")]

    # 이미 있는 파일은 덮어쓰지 않는다.
    again = runner.invoke(app, ["static-db", "get", run.run_id, "--out", str(got)])
    assert again.exit_code == 2
    assert "OUTPUT_EXISTS" in again.output


def test_store_refuses_a_run_whose_raw_report_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 원시 보고서 확인은 DB 연결 전에 일어나므로, 연결 주소만 주고 연결하지 않아야 한다.
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    before, _ = example_runs(REPO)
    folder = tmp_path / "out"
    (folder / "runs").mkdir(parents=True)
    run_file = folder / "runs" / "before-1.static-run.json"
    run_file.write_text(json.dumps(before.model_dump(mode="json")), encoding="utf-8")
    result = CliRunner().invoke(app, ["static-db", "store", str(run_file)])
    assert result.exit_code == 2
    assert "STATIC_RAW_REPORT_MISSING" in result.output


def test_store_refuses_a_result_file_outside_the_runs_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    before, _ = example_runs(REPO)
    run_file = tmp_path / "before-1.static-run.json"
    run_file.write_text(json.dumps(before.model_dump(mode="json")), encoding="utf-8")
    result = CliRunner().invoke(app, ["static-db", "store", str(run_file)])
    assert result.exit_code == 2
    assert "STATIC_RUN_NOT_IN_RUNS_FOLDER" in result.output
