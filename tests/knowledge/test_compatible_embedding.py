import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge.compatible_embedding import CompatibleEmbeddingProvider


def test_compatible_embedding_restores_response_order_normalizes_and_binds_revision(monkeypatch):
    provider = CompatibleEmbeddingProvider("http://localhost:8080/v1", "model", "sha256:fixed")
    monkeypatch.setattr(
        provider.transport,
        "_request",
        lambda *a, **kw: {
            "data": [
                {"index": 1, "embedding": [0.0, 2.0] + [0.0] * 1022},
                {"index": 0, "embedding": [2.0] + [0.0] * 1023},
            ],
        },
    )
    batch = provider.embed(["first", "second"], "DOCUMENT")
    assert batch.vectors[0][0] == batch.vectors[1][1] == 1.0
    assert batch.profile.model_digest == "sha256:fixed"
    assert batch.profile.normalized is True


@pytest.mark.parametrize(
    "vector,index",
    [([0.0] * 1024, 0), ([1.0] * 1023, 0), ([float("nan")] * 1024, 0), ([1.0] * 1024, 4)],
)
def test_invalid_embeddings_are_never_indexed(monkeypatch, vector, index):
    provider = CompatibleEmbeddingProvider("http://localhost:8080/v1", "model", "sha256:fixed")
    monkeypatch.setattr(
        provider.transport,
        "_request",
        lambda *a, **kw: {
            "data": [{"index": index, "embedding": vector}],
        },
    )
    with pytest.raises(InputError, match="EMBEDDING_RESPONSE_INVALID"):
        provider.embed(["text"], "QUERY")
