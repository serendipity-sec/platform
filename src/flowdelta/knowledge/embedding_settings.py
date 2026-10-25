"""Local embedding configuration; secrets are intentionally unsupported for Ollama."""

import os
from dataclasses import dataclass

from flowdelta.ingestion import InputError
from flowdelta.knowledge.embedding import DIMENSIONS, MODEL_NAME, OllamaEmbeddingProvider


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str = "http://127.0.0.1:11434"
    model: str = MODEL_NAME
    dimensions: int = DIMENSIONS
    timeout_seconds: float = 120.0
    max_batch_size: int = 16

    @classmethod
    def from_environment(cls) -> "EmbeddingSettings":
        try:
            dimensions = int(os.environ.get("FLOWDELTA_EMBEDDING_DIMENSIONS", str(DIMENSIONS)))
            timeout = float(os.environ.get("FLOWDELTA_EMBEDDING_TIMEOUT", "120"))
            batch_size = int(os.environ.get("FLOWDELTA_EMBEDDING_BATCH_SIZE", "16"))
        except ValueError:
            raise InputError("EMBEDDING_CONFIGURATION_INVALID") from None
        return cls(
            base_url=os.environ.get("FLOWDELTA_EMBEDDING_BASE_URL", "http://127.0.0.1:11434"),
            model=os.environ.get("FLOWDELTA_EMBEDDING_MODEL", MODEL_NAME),
            dimensions=dimensions,
            timeout_seconds=timeout,
            max_batch_size=batch_size,
        )

    def provider(self) -> OllamaEmbeddingProvider:
        return OllamaEmbeddingProvider(
            self.base_url,
            self.model,
            self.dimensions,
            self.timeout_seconds,
            self.max_batch_size,
        )
