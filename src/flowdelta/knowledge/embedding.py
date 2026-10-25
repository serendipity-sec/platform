"""Provider-neutral embedding contract and bounded Ollama adapter."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from flowdelta.ingestion import InputError, parse_json

EmbeddingPurpose = Literal["DOCUMENT", "QUERY"]
MODEL_NAME = "qwen3-embedding:0.6b"
DIMENSIONS = 1024
QUERY_INSTRUCTION = (
    "Given a vulnerability and workflow-change description, retrieve relevant vulnerability "
    "records or reviewed test scenario cards that apply to the same affected software, version "
    "range, execution context, or security behavior."
)
QUERY_INSTRUCTION_VERSION = hashlib.sha256(QUERY_INSTRUCTION.encode("utf-8")).hexdigest()
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
_MODEL_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


@dataclass(frozen=True)
class EmbeddingProfile:
    """Immutable description of the exact vector space used by an index."""

    provider: str
    model: str
    model_digest: str
    dimensions: int
    query_instruction_version: str
    normalized: bool

    @property
    def profile_id(self) -> str:
        value = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode()
        return "embedding-" + hashlib.sha256(value).hexdigest()[:24]


def embedding_profiles_compatible(left: EmbeddingProfile, right: EmbeddingProfile) -> bool:
    """Return whether two profiles describe the same compatible vector space."""
    return left.profile_id == right.profile_id


@dataclass(frozen=True)
class EmbeddingBatch:
    profile: EmbeddingProfile
    vectors: tuple[tuple[float, ...], ...]


class EmbeddingProvider(Protocol):
    """Implementations accept plain texts and return validated vectors with a profile."""

    def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> EmbeddingBatch: ...


class OllamaEmbeddingProvider:
    """Local-first Ollama HTTP adapter. Request bodies and provider errors are not logged."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = MODEL_NAME,
        dimensions: int = DIMENSIONS,
        timeout_seconds: float = 120.0,
        max_batch_size: int = 16,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise InputError("EMBEDDING_ENDPOINT_INVALID")
        if not model.strip() or not 32 <= dimensions <= 1024:
            raise InputError("EMBEDDING_PROFILE_INVALID")
        if not 0 < timeout_seconds <= 600 or not 1 <= max_batch_size <= 128:
            raise InputError("EMBEDDING_LIMIT_INVALID")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self._max_batch_size = max_batch_size
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def embed(self, texts: list[str], purpose: EmbeddingPurpose) -> EmbeddingBatch:
        if purpose not in {"DOCUMENT", "QUERY"}:
            raise InputError("EMBEDDING_PURPOSE_INVALID")
        if not texts or len(texts) > self._max_batch_size:
            raise InputError("EMBEDDING_BATCH_SIZE_INVALID")
        if any(not isinstance(item, str) or not item.strip() for item in texts):
            raise InputError("EMBEDDING_INPUT_INVALID")
        try:
            input_sizes = [len(item.encode("utf-8")) for item in texts]
        except UnicodeEncodeError:
            raise InputError("EMBEDDING_INPUT_INVALID") from None
        if any(size > MAX_TEXT_BYTES for size in input_sizes):
            raise InputError("EMBEDDING_INPUT_TOO_LARGE")

        if purpose == "QUERY":
            inputs = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {item}" for item in texts]
        else:
            # Qwen3 Embedding recommends instructions for queries, not documents.
            inputs = texts
        response = self._request(
            "POST",
            "/api/embed",
            {
                "model": self._model,
                "input": inputs,
                "dimensions": self._dimensions,
                "truncate": False,
            },
        )
        if response.get("model") != self._model:
            raise InputError("EMBEDDING_MODEL_MISMATCH")
        raw_vectors = response.get("embeddings")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != len(texts):
            raise InputError("EMBEDDING_VECTOR_COUNT_INVALID")
        vectors = tuple(self._validate_vector(item) for item in raw_vectors)
        model_digest = self._resolve_running_digest()
        profile = EmbeddingProfile(
            provider="ollama",
            model=self._model,
            model_digest=model_digest,
            dimensions=self._dimensions,
            query_instruction_version=QUERY_INSTRUCTION_VERSION,
            normalized=True,
        )
        return EmbeddingBatch(profile=profile, vectors=vectors)

    def _resolve_running_digest(self) -> str:
        response = self._request("GET", "/api/ps", None)
        models = response.get("models")
        if not isinstance(models, list):
            raise InputError("EMBEDDING_MODEL_LIST_INVALID")
        matches = [
            item for item in models if isinstance(item, dict) and item.get("name") == self._model
        ]
        if len(matches) != 1:
            raise InputError("EMBEDDING_MODEL_NOT_FOUND")
        digest = matches[0].get("digest")
        if not isinstance(digest, str):
            raise InputError("EMBEDDING_MODEL_DIGEST_INVALID")
        match = _MODEL_DIGEST.fullmatch(digest)
        if match is None:
            raise InputError("EMBEDDING_MODEL_DIGEST_INVALID")
        return f"sha256:{match.group(1)}"

    def _validate_vector(self, value: object) -> tuple[float, ...]:
        if not isinstance(value, list) or len(value) != self._dimensions:
            raise InputError("EMBEDDING_DIMENSION_MISMATCH")
        vector_values: list[float] = []
        for number in value:
            if type(number) not in {int, float}:
                raise InputError("EMBEDDING_VECTOR_INVALID")
            try:
                converted = float(number)
            except OverflowError:
                raise InputError("EMBEDDING_VECTOR_INVALID") from None
            if not math.isfinite(converted):
                raise InputError("EMBEDDING_VECTOR_INVALID")
            vector_values.append(converted)
        vector = tuple(vector_values)
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0.0:
            raise InputError("EMBEDDING_VECTOR_INVALID")
        return tuple(number / norm for number in vector)

    def _request(
        self, method: str, path: str, body: dict[str, object] | None
    ) -> dict[str, JsonValue]:
        try:
            data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
            request = urllib.request.Request(
                self._base_url + path,
                data=data,
                method=method,
                headers={"Content-Type": "application/json"},
            )
        except (UnicodeError, ValueError):
            raise InputError("EMBEDDING_ENDPOINT_INVALID") from None
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise InputError("EMBEDDING_PROVIDER_REJECTED") from None
        except http.client.InvalidURL:
            raise InputError("EMBEDDING_ENDPOINT_INVALID") from None
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ValueError,
        ):
            raise InputError("EMBEDDING_PROVIDER_UNAVAILABLE") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise InputError("EMBEDDING_RESPONSE_TOO_LARGE")
        return parse_json(payload)
