"""Pure composition from a fixed retrieval result and canonical card snapshot."""

from pathlib import Path
from typing import cast

from pydantic import JsonValue

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError
from flowdelta.knowledge.cards import _KnowledgeSnapshot
from flowdelta.knowledge.retrieval import (
    analysis_retrieval_context,
    author,
    context_fingerprint,
    exclusion_reason,
    hit_details,
    retrieval_content_fingerprint,
    retrieval_identity,
    search_context,
)
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ImpactPath,
    ObligationBinding,
    RetrievalResult,
    ScenarioCard,
    Template,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import (
    _validate_pipeline_context,
    validate_input_fields,
    validate_pipeline,
)
from flowdelta.runtime.static_adapter import StaticPreflight, static_preflight
from flowdelta.runtime.static_scan import StaticScanCatalog, StaticScanTemplate
from flowdelta.runtime.static_targets import find_targets


def compose_plan(
    handoff: Handoff,
    analysis: AnalysisBundle,
    cards: list[ScenarioCard],
    catalog: TemplateCatalog | StaticScanCatalog,
    retrieval: RetrievalResult,
    *,
    producer: str,
    handoff_root: Path | None = None,
    repo_root: Path | None = None,
    selected_path_id: str | None = None,
) -> TestPlan:
    """Compose a normal or registered static-scan plan from a fixed retrieval result."""
    return _compose_snapshot(
        handoff,
        analysis,
        _KnowledgeSnapshot.prepare(cards),
        catalog,
        retrieval,
        producer=producer,
        handoff_root=handoff_root,
        repo_root=repo_root,
        selected_path_id=selected_path_id,
    )


def _compose_snapshot(
    handoff: Handoff,
    analysis: AnalysisBundle,
    snapshot: _KnowledgeSnapshot,
    catalog: TemplateCatalog | StaticScanCatalog,
    retrieval: RetrievalResult,
    *,
    producer: str,
    handoff_root: Path | None = None,
    repo_root: Path | None = None,
    selected_path_id: str | None = None,
) -> TestPlan:
    """Validate registered catalog provenance and retrieval before composing its plan."""
    if isinstance(catalog, StaticScanCatalog) and catalog.authorship.origin != "MODULE_OUTPUT":
        raise InputError("STATIC_CATALOG_NOT_REGISTERED")
    cards = snapshot.cards
    if retrieval.analysis_id != analysis.analysis_id:
        raise InputError("RETRIEVAL_ANALYSIS_MISMATCH")
    if (
        retrieval.query.get("rules_version") != producer
        or retrieval.authorship.producer != producer
    ):
        raise InputError("RETRIEVAL_VERSION_MISMATCH")
    if retrieval.query.get("context_fingerprint") != context_fingerprint(
        handoff, analysis, catalog, producer
    ):
        raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    profile, node_types, fields, tokens = search_context(handoff, analysis)
    analysis_context, impacted_node_types, impact_tokens, impact_path_signals = (
        analysis_retrieval_context(handoff, analysis)
    )
    expected_query = {
        "task_id": handoff.task.task_id,
        "runtime_profile": profile,
        "node_types": [str(value) for value in node_types],
        "changed_fields": [str(value) for value in fields],
        "analysis_context": analysis_context,
        "rules_version": producer,
        "context_fingerprint": context_fingerprint(handoff, analysis, catalog, producer),
    }
    recorded_query = {
        key: value for key, value in retrieval.query.items() if key != "result_fingerprint"
    }
    if recorded_query != expected_query:
        raise InputError("RETRIEVAL_CONTEXT_MISMATCH")
    if retrieval.method != "KEYWORD" or retrieval.model_version is not None:
        raise InputError("RETRIEVAL_METHOD_MISMATCH")
    expected = snapshot.reference
    if (retrieval.kb_snapshot_ref.sha256, retrieval.kb_snapshot_ref.size_bytes) != (
        expected.sha256,
        expected.size_bytes,
    ):
        raise InputError("RETRIEVAL_SNAPSHOT_MISMATCH")
    if retrieval.retrieval_id != retrieval_identity(
        producer,
        snapshot.data,
        handoff,
        analysis,
        catalog,
        selected_path_id=selected_path_id,
    ):
        raise InputError("RETRIEVAL_ID_MISMATCH")
    known = {(card.card_id, card.version) for card in cards}
    keys = [(hit.card_id, hit.card_version) for hit in retrieval.hits]
    if len(keys) != len(set(keys)) or not set(keys).issubset(known):
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    hit_keys = {f"{card_id}@{version}" for card_id, version in keys}
    card_keys = {f"{card_id}@{version}" for card_id, version in known}
    excluded_keys = set(retrieval.excluded_reasons)
    if (hit_keys & excluded_keys) or (hit_keys | excluded_keys) != card_keys:
        raise InputError("RETRIEVAL_CARD_MISMATCH")
    if [hit.rank for hit in retrieval.hits] != list(range(1, len(keys) + 1)):
        raise InputError("RETRIEVAL_RANK_INVALID")
    expected_order = sorted(
        retrieval.hits,
        key=lambda hit: (-hit.score, hit.card_id, hit.card_version),
    )
    if retrieval.hits != expected_order:
        raise InputError("RETRIEVAL_RANK_INVALID")
    if any(hit.applicability == "NO_MATCH" for hit in retrieval.hits):
        raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    by_card = {(card.card_id, card.version): card for card in cards}
    node_type_set = set(node_types)
    templates = {
        (template.template_id, template.version): template for template in catalog.templates
    }
    for card in cards:
        key = f"{card.card_id}@{card.version}"
        reason = exclusion_reason(card, handoff, templates, profile, node_type_set)
        if key in excluded_keys:
            if reason is None or reason != retrieval.excluded_reasons[key]:
                raise InputError("RETRIEVAL_CARD_MISMATCH")
        elif reason is not None:
            raise InputError("RETRIEVAL_INELIGIBLE_CARD")
    for hit in retrieval.hits:
        card = by_card[(hit.card_id, hit.card_version)]
        score, applicability, reason = hit_details(
            card,
            node_type_set,
            tokens,
            impacted_node_types=impacted_node_types,
            impact_tokens=impact_tokens,
            impact_path_signals=impact_path_signals,
        )
        if hit.applicability != applicability:
            raise InputError("RETRIEVAL_INELIGIBLE_CARD")
        if hit.score != score or hit.reason != reason:
            raise InputError("RETRIEVAL_CONTENT_MISMATCH")
    if retrieval.query.get("result_fingerprint") != retrieval_content_fingerprint(retrieval):
        raise InputError("RETRIEVAL_CONTENT_MISMATCH")
    if isinstance(catalog, StaticScanCatalog):
        static_candidates = list(catalog.templates)
        if retrieval.hits:
            card = by_card[(retrieval.hits[0].card_id, retrieval.hits[0].card_version)]
            static_candidates = [
                item
                for item in static_candidates
                if (item.template_id, item.version) == (card.template_id, card.template_version)
            ]
        if not static_candidates:
            raise InputError("STATIC_TEMPLATE_NOT_FOUND")
        static_template = sorted(
            static_candidates, key=lambda item: (item.template_id, item.version)
        )[0]
        template: Template | StaticScanTemplate = static_template
        normal_input: dict[str, JsonValue] = {}
        supported_artifact_kinds = static_template.result_kinds
    else:
        normal_candidates = [
            t
            for t in catalog.templates
            if t.task_id == handoff.task.task_id and t.runtime_profile == profile
        ]
        if retrieval.hits:
            card = by_card[(retrieval.hits[0].card_id, retrieval.hits[0].card_version)]
            normal_candidates = [
                item
                for item in normal_candidates
                if (item.template_id, item.version) == (card.template_id, card.template_version)
            ]
        if not normal_candidates:
            raise InputError("TEMPLATE_NOT_FOUND")
        normal_template: Template | None = None
        for candidate in sorted(normal_candidates, key=lambda t: (t.template_id, t.version)):
            try:
                validate_input_fields(handoff.task.normal_input, candidate.input_fields)
            except InputError:
                continue
            normal_template = candidate
            break
        if normal_template is None:
            raise InputError("TEMPLATE_INPUT_INVALID")
        template = normal_template
        normal_input = handoff.task.normal_input
        supported_artifact_kinds = template.required_artifact_kinds
    bindings = []
    uncovered = []
    for obligation in sorted(handoff.case.obligations, key=lambda item: item.obligation_id):
        if set(obligation.required_artifacts).issubset(supported_artifact_kinds):
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
        authorship=author(analysis, producer),
        before_snapshot_id=analysis.before_snapshot_id,
        after_snapshot_id=analysis.after_snapshot_id,
        catalog_id=catalog.catalog_id,
        template_id=template.template_id,
        template_version=template.version,
        normal_input=normal_input,
        initial_state_ref=state.model_copy(update={"path": f"input/{state.path}"}),
        obligation_bindings=bindings,
        uncovered_obligation_ids=uncovered,
        attempts_per_side=1,
        timeout_seconds=template.timeout_seconds,
        planner_metadata={
            "rules_version": producer,
            "retrieval_empty": not retrieval.hits,
            "selection": (
                "ranked-card"
                if retrieval.hits
                else "registered-static-fallback"
                if isinstance(catalog, StaticScanCatalog)
                else "registered-normal-fallback"
            ),
            "analysis_context": analysis_context,
            "security_outcome": None,
        },
    )
    if isinstance(catalog, StaticScanCatalog):
        if handoff_root is None:
            raise InputError("STATIC_HANDOFF_ROOT_REQUIRED")
        repo = repo_root or Path(__file__).resolve().parents[3]
        _validate_pipeline_context(handoff, analysis, plan, catalog.catalog_id)
        preflight = static_preflight(handoff, plan, catalog, handoff_root, repo)
        selection = _select_static_path(analysis, preflight, selected_path_id)
        plan.planner_metadata["selected_path_id"] = selection["selected_path_id"]
        plan.planner_metadata["path_selection"] = selection
    else:
        if selected_path_id is not None:
            raise InputError("PATH_SELECTION_STATIC_ONLY")
        validate_pipeline(handoff, analysis, catalog, plan)
    return plan


def _select_static_path(
    analysis: AnalysisBundle,
    preflight: StaticPreflight,
    selected_path_id: str | None,
) -> dict[str, JsonValue]:
    """Select supported A paths that intersect C targets, preserving incomplete evidence."""
    paths = analysis.impact_paths
    path_ids = [path.path_id for path in paths]
    if len(path_ids) != len(set(path_ids)):
        raise InputError("ANALYSIS_PATH_ID_DUPLICATE")
    targets_by_side = {
        side.side: {
            item.node_id for item in find_targets(side.workflow_bytes, preflight.template).scanned
        }
        for side in preflight.sides
    }
    changes_by_id = {change.change_id: change for change in analysis.changes}
    graphs = {"before": analysis.before_graph, "after": analysis.after_graph}
    nodes_by_side = {
        side: (
            {
                node.node_id: node.workflow_node_id
                for node in graph.nodes
                if node.kind == "WORKFLOW_NODE" and node.workflow_node_id is not None
            }
            if graph.nodes
            else {node_id: node_id for node_id in graph.node_ids}
        )
        for side, graph in graphs.items()
    }
    eligible: dict[str, ImpactPath] = {}
    rejected: list[dict[str, JsonValue]] = []
    for path in paths:
        reason = None
        # A also records explanatory notes on supported paths; preserve them without
        # treating their presence as an unsupported semantic state.
        if path.support != "SUPPORTED":
            reason = "PATH_UNSUPPORTED"
        else:
            graph = graphs[path.side]
            changes = [changes_by_id.get(change_id) for change_id in path.change_ids]
            edges_by_id = {edge.edge_id: edge for edge in graph.edges}
            edges = [edges_by_id.get(edge_id) for edge_id in path.edge_ids]
            if any(change is None for change in changes):
                reason = "PATH_CHANGE_NOT_FOUND"
            elif any(edge is None for edge in edges):
                reason = "PATH_EDGE_NOT_FOUND"
            else:
                workflow_node_ids = set(nodes_by_side[path.side].values())
                path_nodes = {
                    change.node_id
                    for change in changes
                    if change is not None
                    and change.node_id is not None
                    and change.node_id in workflow_node_ids
                }
                for edge in edges:
                    if edge is None:
                        continue
                    path_nodes.update(
                        node_id
                        for node_id in (
                            nodes_by_side[path.side].get(edge.source_node_id),
                            nodes_by_side[path.side].get(edge.target_node_id),
                        )
                        if node_id is not None
                    )
                if not path_nodes & targets_by_side[path.side]:
                    reason = "NO_C_TARGET_MATCH"
        if reason is None:
            eligible[path.path_id] = path
        else:
            rejected.append(
                {
                    "path_id": path.path_id,
                    "reason": reason,
                    "support": path.support,
                    "limitations": list(path.limitations),
                }
            )

    limits = analysis.extensions.get("flowdelta.graph.limits")
    truncated = isinstance(limits, dict) and limits.get("truncated") is True
    selected: ImpactPath | None = None
    if selected_path_id is not None:
        selected = eligible.get(selected_path_id)
        if selected is None:
            if selected_path_id not in path_ids:
                raise InputError("STATIC_PATH_NOT_FOUND")
            raise InputError("STATIC_PATH_NOT_ELIGIBLE")
        status = "EXPLICIT_LIMITED" if truncated else "EXPLICIT"
    elif truncated:
        status = "TRUNCATED"
    elif not paths:
        status = "MISSING"
    elif len(eligible) == 1:
        selected = next(iter(eligible.values()))
        status = "AUTO_SELECTED"
    elif eligible:
        status = "AMBIGUOUS"
    elif all(path.support != "SUPPORTED" for path in paths):
        status = "UNSUPPORTED"
    elif rejected and all(item["reason"] == "NO_C_TARGET_MATCH" for item in rejected):
        status = "NO_C_TARGET_MATCH"
    else:
        status = "NO_ELIGIBLE_PATH"

    return {
        "status": status,
        "selected_path_id": selected.path_id if selected is not None else None,
        "support": selected.support if selected is not None else None,
        "limitations": list(selected.limitations) if selected is not None else [],
        "eligible_path_ids": cast(JsonValue, sorted(eligible)),
        "rejected_paths": cast(JsonValue, rejected),
        "graph_limits": limits,
        "scope": "trace link only; C scans registered supported targets on both workflow sides",
    }
