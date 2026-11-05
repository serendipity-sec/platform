"""Compatible B facade over loading, retrieval, composition and publication."""

from pathlib import Path

from flowdelta.contracts import Handoff
from flowdelta.knowledge.cards import (
    KB_SNAPSHOT_FILENAME,
    _KnowledgeSnapshot,
    kb_bytes,
    load_cards,
)
from flowdelta.knowledge.composition import _compose_snapshot
from flowdelta.knowledge.composition import compose_plan as _compose
from flowdelta.knowledge.output import _write_snapshot, validate_output_paths
from flowdelta.knowledge.retrieval import _retrieve_snapshot as _retrieve
from flowdelta.knowledge.retrieval import retrieve as _retrieve_cards
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.runtime.static_scan import StaticScanCatalog

PRODUCER = "flowdelta.knowledge/1.2.0"
__all__ = [
    "compose_plan",
    "kb_bytes",
    "load_cards",
    "make_plan",
    "retrieve",
    "write_plan",
]


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog | StaticScanCatalog,
    *,
    kb_path: str = KB_SNAPSHOT_FILENAME,
) -> RetrievalResult:
    """Rank cards against either a normal or the registered static-scan catalog."""
    return _retrieve_cards(handoff, analysis, cards, catalog, kb_path=kb_path, producer=PRODUCER)


def compose_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog | StaticScanCatalog,
    retrieval: RetrievalResult,
    *,
    handoff_root: Path | None = None,
    repo_root: Path | None = None,
    selected_path_id: str | None = None,
) -> TestPlan:
    """Consume a fixed search result without repeating retrieval or external I/O."""
    return _compose(
        handoff,
        analysis,
        cards,
        catalog,
        retrieval,
        producer=PRODUCER,
        handoff_root=handoff_root,
        repo_root=repo_root,
        selected_path_id=selected_path_id,
    )


def make_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog | StaticScanCatalog,
    *,
    handoff_root: Path | None = None,
    repo_root: Path | None = None,
    selected_path_id: str | None = None,
) -> TestPlan:
    """Generate a deterministic plan for a normal or registered static-scan catalog."""
    retrieval = retrieve(handoff, analysis, cards, catalog)
    return compose_plan(
        handoff,
        analysis,
        cards,
        catalog,
        retrieval,
        handoff_root=handoff_root,
        repo_root=repo_root,
        selected_path_id=selected_path_id,
    )


def write_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog | StaticScanCatalog,
    output: Path,
    *,
    handoff_root: Path | None = None,
    repo_root: Path | None = None,
    selected_path_id: str | None = None,
) -> TestPlan:
    """Publish retrieval, KB snapshot and plan for normal or registered static-scan planning."""
    kb_path, _, _ = validate_output_paths(output)
    snapshot = _KnowledgeSnapshot.prepare(cards, kb_path.name)
    retrieval = _retrieve(handoff, analysis, snapshot, catalog, producer=PRODUCER)
    plan = _compose_snapshot(
        handoff,
        analysis,
        snapshot,
        catalog,
        retrieval,
        producer=PRODUCER,
        handoff_root=handoff_root,
        repo_root=repo_root,
        selected_path_id=selected_path_id,
    )
    return _write_snapshot(snapshot, retrieval, plan, output)
