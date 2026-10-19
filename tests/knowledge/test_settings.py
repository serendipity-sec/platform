import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge.settings import KnowledgeSettings


def test_default_raw_root_is_absolute_and_independent_of_current_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.delenv("FLOWDELTA_KB_RAW_ROOT", raising=False)
    first = KnowledgeSettings.from_environment().raw_root
    monkeypatch.chdir(tmp_path)
    second = KnowledgeSettings.from_environment().raw_root
    assert first.is_absolute()
    assert first == second


def test_relative_raw_root_is_rejected(monkeypatch):
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.setenv("FLOWDELTA_KB_RAW_ROOT", "var/knowledge/raw")
    with pytest.raises(InputError, match="KNOWLEDGE_RAW_ROOT_ABSOLUTE_REQUIRED"):
        KnowledgeSettings.from_environment()
