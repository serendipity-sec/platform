import json
import os
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
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
    run_file = tmp_path / "run.static-run.json"
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
