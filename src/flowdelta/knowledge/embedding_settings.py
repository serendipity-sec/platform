"""Local embedding configuration; secrets are intentionally unsupported for Ollama."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from flowdelta.ingestion import InputError
from flowdelta.knowledge.embedding import (
    DIMENSIONS,
    MODEL_NAME,
    OllamaEmbeddingProvider,
)

if TYPE_CHECKING:
    from flowdelta.knowledge.compatible_embedding import CompatibleEmbeddingProvider


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str = "http://127.0.0.1:11434"
    model: str = MODEL_NAME
    dimensions: int = DIMENSIONS
    timeout_seconds: float = 120.0
    max_batch_size: int = 16
    provider_name: str = "ollama"
    model_digest: str | None = None
    api_key_env: str | None = None

    @classmethod
    def from_environment(cls) -> EmbeddingSettings:
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
            provider_name=os.environ.get("FLOWDELTA_EMBEDDING_PROVIDER", "ollama"),
            model_digest=os.environ.get("FLOWDELTA_EMBEDDING_MODEL_DIGEST"),
            api_key_env=os.environ.get("FLOWDELTA_EMBEDDING_API_KEY_ENV"),
        )

    def provider(self) -> OllamaEmbeddingProvider | CompatibleEmbeddingProvider:
        if self.provider_name == "openai-compatible":
            from flowdelta.knowledge.compatible_embedding import CompatibleEmbeddingProvider

            if not self.model_digest or self.dimensions != 1024:
                raise InputError("EMBEDDING_CONFIGURATION_INVALID")
            return CompatibleEmbeddingProvider(
                self.base_url,
                self.model,
                self.model_digest,
                api_key_env=self.api_key_env,
                timeout_seconds=int(self.timeout_seconds),
            )
        if self.provider_name != "ollama":
            raise InputError("EMBEDDING_CONFIGURATION_INVALID")
        return OllamaEmbeddingProvider(
            self.base_url,
            self.model,
            self.dimensions,
            self.timeout_seconds,
            self.max_batch_size,
        )
