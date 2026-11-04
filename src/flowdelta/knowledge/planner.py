"""Compatible B facade over loading, retrieval, composition and publication."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from flowdelta.contracts import Handoff
from flowdelta.knowledge.cards import (
    KB_SNAPSHOT_FILENAME,
    _KnowledgeSnapshot,
    kb_bytes,
    load_cards,
)
from flowdelta.knowledge.composition import _compose_snapshot
from flowdelta.knowledge.composition import compose_plan as _compose
from flowdelta.knowledge.embedding import EmbeddingProvider
from flowdelta.knowledge.output import _write_snapshot, validate_output_paths
from flowdelta.knowledge.retrieval import _retrieve_snapshot as _retrieve
from flowdelta.knowledge.retrieval import retrieve as _retrieve_cards
from flowdelta.knowledge.retrieval import retrieve_vector_snapshot
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

PRODUCER = "flowdelta.knowledge/1.2.0"
__all__ = [
    "compose_plan",
    "kb_bytes",
    "load_cards",
    "make_plan",
    "retrieve",
    "write_plan",
    "write_vector_plan",
]


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    *,
    kb_path: str = KB_SNAPSHOT_FILENAME,
) -> RetrievalResult:
    return _retrieve_cards(handoff, analysis, cards, catalog, kb_path=kb_path, producer=PRODUCER)


def compose_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    retrieval: RetrievalResult,
) -> TestPlan:
    """Consume a fixed search result without repeating retrieval or external I/O."""
    return _compose(handoff, analysis, cards, catalog, retrieval, producer=PRODUCER)


def make_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
) -> TestPlan:
    return compose_plan(
        handoff, analysis, cards, catalog, retrieve(handoff, analysis, cards, catalog)
    )


def write_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    output: Path,
) -> TestPlan:
    kb_path, _, _ = validate_output_paths(output)
    snapshot = _KnowledgeSnapshot.prepare(cards, kb_path.name)
    retrieval = _retrieve(handoff, analysis, snapshot, catalog, producer=PRODUCER)
    plan = _compose_snapshot(handoff, analysis, snapshot, catalog, retrieval, producer=PRODUCER)
    return _write_snapshot(snapshot, retrieval, plan, output)


def write_vector_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    output: Path,
    *,
    engine: Engine,
    provider: EmbeddingProvider,
    limit: int = 50,
) -> TestPlan:
    kb_path, _, _ = validate_output_paths(output)
    snapshot, retrieval = retrieve_vector_snapshot(
        handoff,
        analysis,
        catalog,
        engine=engine,
        provider=provider,
        limit=limit,
        kb_path=kb_path.name,
        producer=PRODUCER,
    )
    plan = _compose_snapshot(handoff, analysis, snapshot, catalog, retrieval, producer=PRODUCER)
    return _write_snapshot(snapshot, retrieval, plan, output)
