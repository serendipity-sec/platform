"""1024-dimensional compatible embedding endpoint, with explicit model revision binding."""

import math
from typing import Any

from flowdelta.ingestion import InputError
from flowdelta.knowledge.embedding import (
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingPurpose,
    validate_embedding_batch,
)
from flowdelta.knowledge.generation import HttpPlanningProvider
from flowdelta.knowledge.planning_config import GenerationSettings


class CompatibleEmbeddingProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        model_digest: str,
        *,
        api_key_env: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.transport = HttpPlanningProvider(
            GenerationSettings(
                provider="openai-compatible",
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
                timeout_seconds=timeout_seconds,
                max_request_bytes=128 * 1024,
            )
        )
        self._profile = EmbeddingProfile(
            provider="openai-compatible",
            model=model,
            model_digest=model_digest,
            dimensions=1024,
            query_instruction_version="plain-query-v1",
            normalized=True,
        )

    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> EmbeddingBatch:
        if purpose not in {"QUERY", "DOCUMENT"}:
            raise InputError("EMBEDDING_PURPOSE_INVALID")
        if not 1 <= len(texts) <= 16:
            raise InputError("EMBEDDING_BATCH_SIZE_INVALID")
        try:
            result = self.transport._request(
                "/embeddings", {"model": self._profile.model, "input": list(texts)}
            )
        except InputError as exc:
            raise InputError(exc.code.replace("GENERATION_", "EMBEDDING_")) from None
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise InputError("EMBEDDING_RESPONSE_INVALID")
        by_index: dict[int, tuple[float, ...]] = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("index")) is not int:
                raise InputError("EMBEDDING_RESPONSE_INVALID")
            index: Any = row["index"]
            vector = row.get("embedding")
            if (
                index in by_index
                or not isinstance(vector, list)
                or len(vector) != 1024
                or any(type(value) not in {int, float} for value in vector)
            ):
                raise InputError("EMBEDDING_RESPONSE_INVALID")
            values = tuple(float(value) for value in vector)  # type: ignore[arg-type]
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm == 0:
                raise InputError("EMBEDDING_RESPONSE_INVALID")
            by_index[index] = tuple(value / norm for value in values)
        if set(by_index) != set(range(len(texts))):
            raise InputError("EMBEDDING_RESPONSE_INVALID")
        return validate_embedding_batch(
            EmbeddingBatch(self._profile, tuple(by_index[index] for index in range(len(texts)))),
            expected_count=len(texts),
            dimensions=1024,
        )
