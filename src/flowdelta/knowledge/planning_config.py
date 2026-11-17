"""Explicit, versioned planning inputs; credentials never enter this document."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from flowdelta.contracts import Contract, Identifier, NonEmpty, Sha256
from flowdelta.ingestion import _read_local, parse_file_model, parse_json


class FrozenCard(Contract):
    card_id: Identifier
    version: NonEmpty
    sha256: Sha256


class GenerationSettings(Contract):
    provider: Literal["ollama", "openai-compatible"]
    base_url: NonEmpty
    model: NonEmpty
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")] | None = None
    model_revision: NonEmpty | None = None
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.0
    seed: Annotated[int, Field(ge=0)] | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 120
    max_output_tokens: Annotated[int, Field(ge=128, le=8192)] = 2048
    max_request_bytes: Annotated[int, Field(ge=1024, le=128 * 1024)] = 32 * 1024


class SelectionBudget(Contract):
    max_test_pairs: Annotated[int, Field(ge=1, le=100)] = 4
    max_c_runs: Annotated[int, Field(ge=2, le=200)] = 8
    max_runtime_seconds: Annotated[int, Field(ge=1, le=7200)] = 1800


class PlanningConfig(Contract):
    config_format: Literal["flowdelta-planning-config-v1"] = "flowdelta-planning-config-v1"
    retrieval: Literal["KEYWORD", "VECTOR"] = "KEYWORD"
    search_limit: Annotated[int, Field(ge=1, le=100)] = 50
    embedding_profile_id: NonEmpty | None = None
    frozen_cards: Annotated[list[FrozenCard], Field(max_length=1000)] = Field(default_factory=list)
    generation: GenerationSettings | None = None
    budget: SelectionBudget = Field(default_factory=SelectionBudget)

    @model_validator(mode="after")
    def fixed_search(self) -> Self:
        keys = [(item.card_id, item.version) for item in self.frozen_cards]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate frozen card")
        if self.retrieval == "VECTOR" and (not keys or not self.embedding_profile_id):
            raise ValueError("vector planning requires frozen cards and embedding profile")
        if self.retrieval == "KEYWORD" and (keys or self.embedding_profile_id):
            raise ValueError("keyword planning uses explicit local cards")
        return self


def read_planning_config(path: Path) -> PlanningConfig:
    return parse_file_model(
        PlanningConfig,
        parse_json(_read_local(path.absolute().parent.resolve(strict=True), path.name)),
    )
