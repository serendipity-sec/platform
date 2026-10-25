import builtins
import json

from typer.testing import CliRunner

from flowdelta.knowledge.cli import app


def _clear_settings(monkeypatch):
    for name in (
        "FLOWDELTA_KB_DATABASE_URL",
        "FLOWDELTA_KB_MIGRATION_URL",
        "FLOWDELTA_KB_RAW_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_status_reports_missing_configuration_as_json(monkeypatch):
    _clear_settings(monkeypatch)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 2
    assert json.loads(result.output) == {"error": "KNOWLEDGE_DATABASE_URL_REQUIRED"}


def test_status_reports_missing_knowledge_extra_as_json(monkeypatch):
    _clear_settings(monkeypatch)
    original_import = builtins.__import__

    def fail_database_import(name, *args, **kwargs):
        if name == "flowdelta.knowledge.database":
            raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_database_import)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 2
    assert json.loads(result.output) == {"error": "KNOWLEDGE_EXTRA_REQUIRED"}
