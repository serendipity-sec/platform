import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge.embedding import (
    DIMENSIONS,
    MAX_TEXT_BYTES,
    MODEL_NAME,
    QUERY_INSTRUCTION,
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingProvider,
    EmbeddingPurpose,
    OllamaEmbeddingProvider,
)

_DIGEST_HEX = "a" * 64
_DIGEST = "sha256:" + _DIGEST_HEX


class MockProvider:
    def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> EmbeddingBatch:
        return EmbeddingBatch(
            EmbeddingProfile("mock", "mock-v1", _DIGEST, 2, "mock", False),
            tuple((1.0, 0.0) for _ in texts),
        )


class Handler(BaseHTTPRequestHandler):
    tags: object = {"models": [{"name": MODEL_NAME, "digest": _DIGEST_HEX}]}
    vectors: object = [[0.25] * DIMENSIONS]
    fail_with: int | None = None
    delay_seconds = 0.0
    requests: list[tuple[str, object]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value, allow_nan=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        type(self).requests.append((self.path, None))
        if type(self).delay_seconds:
            time.sleep(type(self).delay_seconds)
        if self.fail_with:
            self._send({"error": "fixture error"}, self.fail_with)
        else:
            self._send(type(self).tags)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(raw)
        type(self).requests.append((self.path, request))
        vectors = type(self).vectors
        if isinstance(vectors, list) and vectors and isinstance(vectors[0], list):
            vectors = vectors * len(request["input"])
        self._send({"model": MODEL_NAME, "embeddings": vectors})


@pytest.fixture
def fake_ollama():
    Handler.tags = {"models": [{"name": MODEL_NAME, "digest": _DIGEST_HEX}]}
    Handler.vectors = [[0.25] * DIMENSIONS]
    Handler.fail_with = None
    Handler.delay_seconds = 0.0
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def provider(base_url: str) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(base_url, timeout_seconds=2)


def test_provider_protocol_and_query_document_roles(fake_ollama):
    mock: EmbeddingProvider = MockProvider()
    assert len(mock.embed(["safe synthetic text"], "DOCUMENT").vectors) == 1

    ollama = provider(fake_ollama)
    document = ollama.embed(["unmodified document"], "DOCUMENT")
    query = ollama.embed(["workflow changed"], "QUERY")
    assert document.profile.provider == "ollama"
    assert document.profile.model_digest == _DIGEST
    assert document.profile.dimensions == DIMENSIONS
    assert document.profile.normalized is True
    assert sum(value * value for value in document.vectors[0]) ** 0.5 == pytest.approx(1.0)
    assert document.profile.profile_id != query.profile.profile_id
    requests = [item for path, item in Handler.requests if path == "/api/embed"]
    assert requests[0]["input"] == ["unmodified document"]
    assert requests[0]["truncate"] is False
    assert requests[0]["dimensions"] == DIMENSIONS
    assert requests[1]["input"] == [f"Instruct: {QUERY_INSTRUCTION}\nQuery: workflow changed"]


@pytest.mark.parametrize(
    ("tags", "code"),
    [
        ({"models": []}, "EMBEDDING_MODEL_NOT_FOUND"),
        ({"models": [{"name": MODEL_NAME, "digest": "mutable"}]}, "EMBEDDING_MODEL_DIGEST_INVALID"),
        ({"models": None}, "EMBEDDING_MODEL_LIST_INVALID"),
    ],
)
def test_model_catalog_is_validated(fake_ollama, tags, code):
    Handler.tags = tags
    with pytest.raises(InputError, match=code):
        provider(fake_ollama).embed(["synthetic"], "DOCUMENT")


@pytest.mark.parametrize(
    ("vectors", "code"),
    [
        ([], "EMBEDDING_VECTOR_COUNT_INVALID"),
        ([[1.0]], "EMBEDDING_DIMENSION_MISMATCH"),
        ([[float("nan")] * DIMENSIONS], "EMBEDDING_VECTOR_INVALID"),
        ([[0.0] * DIMENSIONS], "EMBEDDING_VECTOR_INVALID"),
    ],
)
def test_embedding_response_is_validated(fake_ollama, vectors, code):
    Handler.vectors = vectors
    with pytest.raises(InputError, match=code):
        provider(fake_ollama).embed(["synthetic"], "DOCUMENT")


def test_request_limits_and_invalid_input(fake_ollama):
    with pytest.raises(InputError, match="EMBEDDING_BATCH_SIZE_INVALID"):
        OllamaEmbeddingProvider(fake_ollama, max_batch_size=1).embed(["one", "two"], "DOCUMENT")
    with pytest.raises(InputError, match="EMBEDDING_INPUT_INVALID"):
        provider(fake_ollama).embed(["  "], "DOCUMENT")
    with pytest.raises(InputError, match="EMBEDDING_INPUT_TOO_LARGE"):
        provider(fake_ollama).embed(["x" * (MAX_TEXT_BYTES + 1)], "DOCUMENT")
    with pytest.raises(InputError, match="EMBEDDING_ENDPOINT_INVALID"):
        OllamaEmbeddingProvider("".join(("http://", "synthetic-user", "@", "example.invalid")))


def test_provider_timeout_is_classified(fake_ollama):
    Handler.delay_seconds = 0.05
    with pytest.raises(InputError, match="EMBEDDING_PROVIDER_UNAVAILABLE"):
        OllamaEmbeddingProvider(fake_ollama, timeout_seconds=0.001).embed(
            ["safe synthetic text"], "DOCUMENT"
        )


def test_provider_errors_do_not_echo_server_body(fake_ollama):
    Handler.fail_with = 500
    with pytest.raises(InputError, match="EMBEDDING_PROVIDER_REJECTED") as error:
        provider(fake_ollama).embed(["private synthetic payload"], "DOCUMENT")
    assert "fixture error" not in str(error.value)
    assert "private synthetic payload" not in str(error.value)


def test_cli_smoke_never_emits_text_or_vectors(monkeypatch):
    from typer.testing import CliRunner

    from flowdelta.cli import app
    from flowdelta.knowledge.embedding_settings import EmbeddingSettings

    monkeypatch.setattr(EmbeddingSettings, "provider", lambda self: MockProvider())
    result = CliRunner().invoke(app, ["embedding-smoke"])
    assert result.exit_code == 0, result.output
    assert "sample_content_emitted" in result.output
    assert "vector_contents_emitted" in result.output
    assert "Synthetic advisory record" not in result.output
    assert "Synthetic workflow change" not in result.output
    assert "[1.0" not in result.output
