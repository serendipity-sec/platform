"""Offline B producer. Cards are data; only catalog templates become plans."""

import re
from pathlib import Path

from flowdelta.contracts import ArtifactRef, Handoff
from flowdelta.ingestion import (
    MAX_TOTAL_BYTES,
    InputError,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    Authorship,
    ObligationBinding,
    RetrievalHit,
    RetrievalResult,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import validate_pipeline, verify_ref

PRODUCER = "flowdelta.knowledge/1.0.0"


def _author(analysis: AnalysisBundle) -> Authorship:
    return Authorship(
        origin="MODULE_OUTPUT",
        producer=PRODUCER,
        note=f"Deterministic local planning; analysis origin={analysis.authorship.origin}.",
    )


def _unique(cards: list[ScenarioCard]) -> list[ScenarioCard]:
    keys = [(card.card_id, card.version) for card in cards]
    if len(keys) != len(set(keys)):
        raise InputError("DUPLICATE_CARD_VERSION")
    return sorted(cards, key=lambda card: (card.card_id, card.version))


def load_cards(directory: Path, *, bundle_root: Path | None = None) -> list[ScenarioCard]:
    """Read bounded local JSON cards; external source refs use the whole bundle root."""
    if not directory.is_dir() or directory.is_symlink():
        raise InputError("CARD_DIRECTORY_INVALID")
    root = directory.resolve()
    cards = []
    total = 0
    for path in sorted(root.glob("*.json")):
        data = _read_local(root, path.name)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")
        card = parse_file_model(ScenarioCard, parse_json(data))
        if card.source_ref is not None:
            if bundle_root is None:
                raise InputError("CARD_SOURCE_ROOT_REQUIRED")
            verify_ref(bundle_root.resolve(), card.source_ref)
        cards.append(card)
    return _unique(cards)


def _ref(path: str, data: bytes, artifact_id: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        path=path,
        sha256=digest(data),
        size_bytes=len(data),
        kind="generated",
    )


def kb_bytes(cards: list[ScenarioCard]) -> bytes:
    """Embed the exact parsed cards, including provenance, in an immutable snapshot."""
    return json_bytes(
        {
            "schema_version": "0.4.0",
            "cards": [
                {
                    "sha256": digest(json_bytes(card.model_dump(mode="json"))),
                    "card": card.model_dump(mode="json"),
                }
                for card in _unique(cards)
            ],
            "hash_basis": "canonical-json-v1",
        }
    )


def retrieve(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    *,
    kb_path: str = "kb-snapshot.json",
) -> RetrievalResult:
    cards = _unique(cards)
    profile = handoff.environment.extensions.get("flowdelta.semantic_profile")
    node_types = sorted(
        {node.node_type for wf in (handoff.before, handoff.after) for node in wf.nodes}
    )
    fields = sorted(
        {
            source.json_pointer.rsplit("/", 1)[-1]
            for change in analysis.changes
            for source in (change.before_source, change.after_source)
            if source is not None
        }
    )
    tokens = sorted(
        {token.casefold() for field in fields for token in re.findall(r"[\w-]+", field)}
    )
    templates = {(t.template_id, t.version): t for t in catalog.templates}
    excluded: dict[str, str] = {}
    candidates: list[tuple[ScenarioCard, float, str]] = []
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        template = templates.get((card.template_id, card.template_version))
        if card.runtime_profile != profile:
            excluded[key] = "RUNTIME_PROFILE_MISMATCH"
            continue
        if template is None:
            raise InputError("TEMPLATE_NOT_FOUND")
        if template.task_id != handoff.task.task_id or template.runtime_profile != profile:
            excluded[key] = "TEMPLATE_CONTEXT_MISMATCH"
            continue
        overlap = sorted(set(card.node_types) & set(node_types))
        if card.node_types and not overlap:
            excluded[key] = "NODE_TYPE_MISMATCH"
            continue
        text = f"{card.title} {card.summary}".casefold()
        matched = [token for token in tokens if token in text]
        score = float(len(overlap) + len(matched))
        candidates.append(
            (card, score, f"task/profile matched; node_types={overlap}; field_keywords={matched}")
        )
    candidates.sort(key=lambda item: (-item[1], item[0].card_id, item[0].version))
    kb = kb_bytes(cards)
    fingerprint = digest(
        kb
        + json_bytes(analysis.model_dump(mode="json"))
        + json_bytes(catalog.model_dump(mode="json"))
        + json_bytes(handoff.model_dump(mode="json"))
    )[:24]
    return RetrievalResult(
        retrieval_id=f"retrieval-{fingerprint}",
        analysis_id=analysis.analysis_id,
        authorship=_author(analysis),
        kb_snapshot_ref=_ref(kb_path, kb, "knowledge-snapshot"),
        method="KEYWORD",
        model_version=None,
        query={
            "task_id": handoff.task.task_id,
            "runtime_profile": profile,
            "node_types": [str(value) for value in node_types],
            "changed_fields": [str(value) for value in fields],
            "rules_version": PRODUCER,
        },
        hits=[
            RetrievalHit(
                card_id=card.card_id,
                card_version=card.version,
                rank=rank,
                score=score,
                applicability="MATCH" if set(card.node_types).issubset(node_types) else "PARTIAL",
                reason=reason,
            )
            for rank, (card, score, reason) in enumerate(candidates, 1)
        ],
        excluded_reasons=excluded,
    )


def make_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
) -> TestPlan:
    retrieval = retrieve(handoff, analysis, cards, catalog)
    by_card = {(card.card_id, card.version): card for card in cards}
    profile = handoff.environment.extensions.get("flowdelta.semantic_profile")
    eligible = [
        t
        for t in catalog.templates
        if t.task_id == handoff.task.task_id and t.runtime_profile == profile
    ]
    if retrieval.hits:
        card = by_card[(retrieval.hits[0].card_id, retrieval.hits[0].card_version)]
        eligible = [
            t
            for t in eligible
            if (t.template_id, t.version) == (card.template_id, card.template_version)
        ]
    if not eligible:
        raise InputError("TEMPLATE_NOT_FOUND")
    template = sorted(eligible, key=lambda t: (t.template_id, t.version))[0]
    bindings = []
    uncovered = []
    for obligation in sorted(handoff.case.obligations, key=lambda item: item.obligation_id):
        if set(obligation.required_artifacts).issubset(template.required_artifact_kinds):
            bindings.append(
                ObligationBinding(
                    obligation_id=obligation.obligation_id,
                    required_artifact_kinds=sorted(obligation.required_artifacts),
                )
            )
        else:
            uncovered.append(obligation.obligation_id)
    state = next(
        ref
        for ref in handoff.source_manifest.artifacts
        if ref.path == handoff.case.initial_state_ref
    )
    plan = TestPlan(
        plan_id=f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}",
        case_id=handoff.case.case_id,
        analysis_id=analysis.analysis_id,
        authorship=_author(analysis),
        before_snapshot_id=analysis.before_snapshot_id,
        after_snapshot_id=analysis.after_snapshot_id,
        catalog_id=catalog.catalog_id,
        template_id=template.template_id,
        template_version=template.version,
        normal_input=handoff.task.normal_input,
        initial_state_ref=state.model_copy(update={"path": f"input/{state.path}"}),
        obligation_bindings=bindings,
        uncovered_obligation_ids=uncovered,
        attempts_per_side=1,
        timeout_seconds=template.timeout_seconds,
        planner_metadata={
            "rules_version": PRODUCER,
            "retrieval_empty": not retrieval.hits,
            "selection": "ranked-card" if retrieval.hits else "registered-normal-fallback",
            "security_outcome": None,
        },
    )
    validate_pipeline(handoff, analysis, catalog, plan)
    return plan


def write_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog,
    output: Path,
) -> TestPlan:
    """Write new sibling KB/retrieval files; publish plan last. No bundle manifest."""
    if not output.parent.is_dir():
        raise InputError("OUTPUT_PARENT_MISSING")
    kb_path = output.with_name("kb-snapshot.json")
    retrieval_path = output.with_name("retrieval.json")
    paths = [kb_path, retrieval_path, output]
    if len(set(paths)) != len(paths):
        raise InputError("OUTPUT_PATH_COLLISION")
    if any(path.exists() or path.is_symlink() for path in paths):
        raise InputError("OUTPUT_EXISTS")
    retrieval = retrieve(handoff, analysis, cards, catalog, kb_path=kb_path.name)
    plan = make_plan(handoff, analysis, cards, catalog)
    retrieval_data = json_bytes(retrieval.model_dump(mode="json"))
    plan.retrieval_ref = _ref(retrieval_path.name, retrieval_data, retrieval.retrieval_id)
    created = []
    try:
        for path, data in zip(
            paths,
            [kb_bytes(cards), retrieval_data, json_bytes(plan.model_dump(mode="json"))],
            strict=True,
        ):
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise InputError("OUTPUT_WRITE_ERROR") from exc
    return plan
