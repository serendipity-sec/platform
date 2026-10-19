"""Compatible B facade over loading, retrieval, composition and publication."""

from pathlib import Path

from flowdelta.contracts import Handoff
from flowdelta.knowledge.cards import kb_bytes, load_cards
from flowdelta.knowledge.composition import compose_plan as _compose
from flowdelta.knowledge.output import validate_output_paths, write_artifacts
from flowdelta.knowledge.retrieval import retrieve as _retrieve
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)

PRODUCER = "flowdelta.knowledge/1.1.0"
__all__ = ["compose_plan", "kb_bytes", "load_cards", "make_plan", "retrieve", "write_plan"]


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    *,
    kb_path: str = "kb-snapshot.json",
) -> RetrievalResult:
    return _retrieve(handoff, analysis, cards, catalog, kb_path=kb_path, producer=PRODUCER)


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
    validate_output_paths(output)
    retrieval = retrieve(handoff, analysis, cards, catalog)
    plan = compose_plan(handoff, analysis, cards, catalog, retrieval)
    return write_artifacts(cards, retrieval, plan, output)
