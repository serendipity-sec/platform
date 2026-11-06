import builtins
import json
from types import SimpleNamespace

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


def test_index_cards_uses_verified_bundle_and_read_only_profile(monkeypatch, tmp_path):
    from flowdelta.knowledge import card_catalog, cards
    from flowdelta.knowledge.embedding import EmbeddingProfile
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    bundle = tmp_path / "bundle"
    card_dir = bundle / "cards"
    card = object()
    profile = EmbeddingProfile("ollama", "model", "sha256:" + "a" * 64, 1024, "query-v1", True)
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    disposed = []
    indexed = []
    loaded = []

    class Provider:
        def profile(self):
            return profile

        def embed(self, texts, purpose):
            raise AssertionError("profile resolution must not embed card documents")

    provider = Provider()
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://app:secret@db/kb")
    monkeypatch.setattr(
        cards,
        "load_cards",
        lambda path, *, bundle_root: loaded.append((path, bundle_root)) or [card],
    )
    monkeypatch.setattr(EmbeddingSettings, "provider", lambda self: provider)
    monkeypatch.setattr("flowdelta.knowledge.database.engine_for", lambda settings: engine)
    monkeypatch.setattr(
        card_catalog,
        "index_cards",
        lambda actual_engine, actual_cards, actual_provider, expected, *, batch_size: (
            indexed.append((actual_engine, actual_cards, actual_provider, expected, batch_size))
            or SimpleNamespace(indexed=1, reused=0)
        ),
    )

    result = CliRunner().invoke(
        app,
        ["index-cards", str(bundle), "--cards", str(card_dir)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"status": "CARDS_INDEXED", "indexed": 1, "reused": 0}
    assert loaded == [(card_dir, bundle.resolve())]
    assert indexed == [(engine, [card], provider, profile, 16)]
    assert disposed == [True]
    assert "secret" not in result.output
    assert "sha256:" not in result.output


def test_index_cards_passes_smaller_configured_embedding_batch(monkeypatch, tmp_path):
    from flowdelta.knowledge import card_catalog, cards
    from flowdelta.knowledge.embedding import EmbeddingProfile
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    bundle = tmp_path / "bundle"
    card_dir = bundle / "cards"
    profile = EmbeddingProfile("ollama", "model", "sha256:" + "a" * 64, 1024, "query-v1", True)
    settings = EmbeddingSettings(max_batch_size=4)
    engine = SimpleNamespace(dispose=lambda: None)
    received_batch_sizes = []

    class Provider:
        def profile(self):
            return profile

    provider = Provider()
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://app:secret@db/kb")
    monkeypatch.setattr(cards, "load_cards", lambda path, *, bundle_root: [])
    monkeypatch.setattr(EmbeddingSettings, "from_environment", classmethod(lambda cls: settings))
    monkeypatch.setattr(EmbeddingSettings, "provider", lambda self: provider)
    monkeypatch.setattr("flowdelta.knowledge.database.engine_for", lambda actual: engine)
    monkeypatch.setattr(
        card_catalog,
        "index_cards",
        lambda actual_engine, actual_cards, actual_provider, expected, *, batch_size: (
            received_batch_sizes.append(batch_size) or SimpleNamespace(indexed=0, reused=0)
        ),
    )

    result = CliRunner().invoke(app, ["index-cards", str(bundle), "--cards", str(card_dir)])

    assert result.exit_code == 0, result.output
    assert received_batch_sizes == [4]


def test_index_cards_reports_missing_knowledge_extra_as_json(monkeypatch, tmp_path):
    _clear_settings(monkeypatch)
    original_import = builtins.__import__

    def fail_database_import(name, *args, **kwargs):
        if name == "flowdelta.knowledge.database":
            raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_database_import)
    result = CliRunner().invoke(
        app,
        ["index-cards", str(tmp_path / "bundle"), "--cards", str(tmp_path / "cards")],
    )

    assert result.exit_code == 2
    assert json.loads(result.output) == {"error": "KNOWLEDGE_EXTRA_REQUIRED"}
