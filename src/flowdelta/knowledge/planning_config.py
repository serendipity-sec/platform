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


class PlanningConfig(Contract):
    config_format: Literal["flowdelta-planning-config-v1"] = "flowdelta-planning-config-v1"
    retrieval: Literal["KEYWORD", "VECTOR"] = "KEYWORD"
    search_limit: Annotated[int, Field(ge=1, le=100)] = 50
    embedding_profile_id: NonEmpty | None = None
    frozen_cards: Annotated[list[FrozenCard], Field(max_length=1000)] = Field(default_factory=list)

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
