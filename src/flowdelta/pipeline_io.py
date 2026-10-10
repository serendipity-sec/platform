"""Read typed handoffs and check references between separately implemented modules."""

import re
from pathlib import Path

from pydantic import JsonValue

from flowdelta.contracts import (
    ArtifactRef,
    ComparisonCase,
    Environment,
    Handoff,
    InitialState,
    Manifest,
    NormalizedWorkflow,
    Policy,
    Provenance,
    SemanticsIndex,
    SnapshotIndex,
    TaskContract,
)
from flowdelta.ingestion import (
    MAX_TOTAL_BYTES,
    InputError,
    _read_local,
    digest,
    parse_json,
    parse_model,
)
from flowdelta.pipeline_contracts import AnalysisBundle, PipelineExamples, TemplateCatalog, TestPlan


def read_manifest(root: Path) -> tuple[Manifest, dict[str, dict[str, JsonValue]]]:
    root = root.resolve(strict=True)
    manifest = parse_model(Manifest, parse_json(_read_local(root, "manifest.json")))
    documents = {}
    total = 0
    for ref in manifest.artifacts:
        data = _read_local(root, ref.path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")
        if len(data) != ref.size_bytes or digest(data) != ref.sha256:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        if ref.media_type != "application/json":
            raise InputError("JSON_SOURCE_REQUIRED")
        documents[ref.path] = parse_json(data)
    return manifest, documents


def read_handoff(bundle: Path) -> Handoff:
    """A consumes the files as written, without running the source workflow."""
    manifest, docs = read_manifest(bundle)
    try:
        case = parse_model(ComparisonCase, docs["case.json"])
        source_manifest = parse_model(Manifest, docs["source-manifest.json"])
        expected = {
            "case.json",
            *case.references(),
            "source-manifest.json",
            "snapshots.json",
            "before.normalized.json",
            "after.normalized.json",
            "semantics.json",
        }
        if set(docs) != expected:
            raise InputError("HANDOFF_FILE_SET_MISMATCH")
        source_refs = [ref for ref in manifest.artifacts if ref.kind == "source"]
        if source_refs != source_manifest.artifacts:
            raise InputError("SOURCE_MANIFEST_MISMATCH")
        result = Handoff(
            case=case,
            task=parse_model(TaskContract, docs[case.task_ref]),
            policy=parse_model(Policy, docs[case.evaluation_policy_ref]),
            environment=parse_model(Environment, docs[case.environment_ref]),
            initial_state=parse_model(InitialState, docs[case.initial_state_ref]),
            provenance=parse_model(Provenance, docs[case.provenance_ref]),
            source_manifest=source_manifest,
            snapshots=parse_model(SnapshotIndex, docs["snapshots.json"]),
            before=parse_model(NormalizedWorkflow, docs["before.normalized.json"]),
            after=parse_model(NormalizedWorkflow, docs["after.normalized.json"]),
            semantics=parse_model(SemanticsIndex, docs["semantics.json"]),
        )
    except KeyError as exc:
        raise InputError("HANDOFF_FILE_MISSING") from exc
    snapshots = result.snapshots.snapshots
    if [item.side for item in snapshots] != ["before", "after"]:
        raise InputError("SNAPSHOT_SIDES_MISMATCH")
    refs = {ref.path: ref for ref in source_refs}
    for snapshot, path in zip(snapshots, [case.before_ref, case.after_ref], strict=True):
        if snapshot.case_id != case.case_id or snapshot.workflow_ref != refs[path]:
            raise InputError("SNAPSHOT_SOURCE_MISMATCH")
    return result


def verify_ref(root: Path, ref: ArtifactRef) -> None:
    data = _read_local(root.resolve(strict=True), ref.path)
    if len(data) != ref.size_bytes or digest(data) != ref.sha256:
        raise InputError("ARTIFACT_INTEGRITY_MISMATCH")


def _same(actual: object, expected: object, code: str) -> None:
    if actual != expected:
        raise InputError(code)


def validate_pipeline(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    plan: TestPlan,
) -> None:
    """Cross-file contracts, independent of A/B's future generation algorithms."""
    case = handoff.case
    before, after = handoff.snapshots.snapshots
    _same((analysis.case_id, plan.case_id), (case.case_id, case.case_id), "CASE_ID_MISMATCH")
    expected_snapshots = (before.snapshot_id, after.snapshot_id)
    _same(
        (analysis.before_snapshot_id, analysis.after_snapshot_id),
        expected_snapshots,
        "SNAPSHOT_ID_MISMATCH",
    )
    _same(
        (plan.before_snapshot_id, plan.after_snapshot_id),
        expected_snapshots,
        "SNAPSHOT_ID_MISMATCH",
    )
    _same(plan.analysis_id, analysis.analysis_id, "ANALYSIS_ID_MISMATCH")
    _same(plan.catalog_id, catalog.catalog_id, "CATALOG_ID_MISMATCH")
    templates = [
        item
        for item in catalog.templates
        if (item.template_id, item.version) == (plan.template_id, plan.template_version)
    ]
    if len(templates) != 1:
        raise InputError("TEMPLATE_NOT_FOUND")
    template = templates[0]
    _same(template.task_id, handoff.task.task_id, "TASK_ID_MISMATCH")
    _same(
        template.runtime_profile,
        handoff.environment.extensions.get("flowdelta.semantic_profile"),
        "RUNTIME_PROFILE_MISMATCH",
    )
    if plan.timeout_seconds > template.timeout_seconds:
        raise InputError("TEMPLATE_TIMEOUT_EXCEEDED")
    fields = {item.name: item for item in template.input_fields}
    if set(plan.normal_input) - set(fields) or any(
        item.required and item.name not in plan.normal_input for item in fields.values()
    ):
        raise InputError("TEMPLATE_INPUT_FIELDS_MISMATCH")
    for name, value in plan.normal_input.items():
        if not isinstance(value, str) or re.fullmatch(fields[name].pattern, value) is None:
            raise InputError("TEMPLATE_INPUT_INVALID")
    _same(plan.normal_input, handoff.task.normal_input, "NORMAL_INPUT_MISMATCH")
    by_path = {ref.path: ref for ref in handoff.source_manifest.artifacts}
    for actual, path in [
        (analysis.task_ref, case.task_ref),
        (analysis.policy_ref, case.evaluation_policy_ref),
        (plan.initial_state_ref, case.initial_state_ref),
    ]:
        expected = by_path[path].model_copy(update={"path": f"input/{path}"})
        _same(actual, expected, "CONTEXT_REF_MISMATCH")
    required = {item.obligation_id: item for item in case.obligations if item.required}
    _same(set(analysis.obligation_ids), set(required), "ANALYSIS_OBLIGATION_MISMATCH")
    bound = {item.obligation_id: item for item in plan.obligation_bindings}
    covered = set(bound) | set(plan.uncovered_obligation_ids)
    _same(covered, set(required), "REQUIRED_OBLIGATION_MISSING")
    for obligation_id, binding in bound.items():
        if not set(required[obligation_id].required_artifacts).issubset(
            binding.required_artifact_kinds
        ):
            raise InputError("OBLIGATION_ARTIFACT_MISSING")
        if not set(binding.required_artifact_kinds).issubset(template.required_artifact_kinds):
            raise InputError("TEMPLATE_ARTIFACT_MISSING")
    _same(
        set(analysis.before_graph.node_ids),
        {node.node_id for node in handoff.before.nodes},
        "GRAPH_NODE_MISMATCH",
    )
    _same(
        set(analysis.after_graph.node_ids),
        {node.node_id for node in handoff.after.nodes},
        "GRAPH_NODE_MISMATCH",
    )


def read_pipeline_examples(root: Path) -> PipelineExamples:
    from flowdelta.pipeline_contracts import ExecutionRun, ScenarioCard

    _, docs = read_manifest(root)
    handoff = read_handoff(root / "input")
    try:
        examples = PipelineExamples(
            analysis=parse_model(AnalysisBundle, docs["analysis.json"]),
            catalog=parse_model(TemplateCatalog, docs["template-catalog.json"]),
            plan=parse_model(TestPlan, docs["plan.json"]),
            run=parse_model(ExecutionRun, docs["run.example.json"]),
            cards=[
                parse_model(ScenarioCard, value)
                for name, value in sorted(docs.items())
                if name.startswith("cards/")
            ],
        )
    except KeyError as exc:
        raise InputError("PIPELINE_FILE_MISSING") from exc
    validate_pipeline(handoff, examples.analysis, examples.catalog, examples.plan)
    for ref in [
        examples.analysis.task_ref,
        examples.analysis.policy_ref,
        examples.plan.initial_state_ref,
    ]:
        verify_ref(root, ref)
    for author in [
        examples.analysis.authorship,
        examples.catalog.authorship,
        examples.plan.authorship,
        examples.run.authorship,
    ]:
        _same(author.origin, "CONTRACT_EXAMPLE", "EXAMPLE_AUTHORSHIP_REQUIRED")
    _same(examples.run.case_id, handoff.case.case_id, "CASE_ID_MISMATCH")
    _same(examples.run.plan_id, examples.plan.plan_id, "PLAN_ID_MISMATCH")
    _same(examples.run.execution_status, "NOT_RUN", "EXAMPLE_MUST_NOT_CLAIM_EXECUTION")
    _same(examples.run.snapshot_id, examples.plan.before_snapshot_id, "SNAPSHOT_ID_MISMATCH")
    _same(
        examples.run.initial_state_sha256,
        examples.plan.initial_state_ref.sha256,
        "STATE_HASH_MISMATCH",
    )
    source_docs = {
        ref.artifact_id: docs[f"input/{ref.path}"] for ref in handoff.source_manifest.artifacts
    }
    for change in examples.analysis.changes:
        for source, value in [
            (change.before_source, change.before_value),
            (change.after_source, change.after_value),
        ]:
            if source is not None:
                _same(
                    resolve_pointer(source_docs.get(source.artifact_id), source.json_pointer),
                    value,
                    "SOURCE_VALUE_MISMATCH",
                )
    for graph in [examples.analysis.before_graph, examples.analysis.after_graph]:
        for edge in graph.edges:
            for source in edge.evidence:
                resolve_pointer(source_docs.get(source.artifact_id), source.json_pointer)
    for card in examples.cards:
        _same(card.authorship.origin, "CONTRACT_EXAMPLE", "EXAMPLE_AUTHORSHIP_REQUIRED")
        _same(
            (card.template_id, card.template_version),
            (examples.plan.template_id, examples.plan.template_version),
            "TEMPLATE_NOT_FOUND",
        )
        _same(card.runtime_profile, examples.run.runtime_profile, "RUNTIME_PROFILE_MISMATCH")
    return examples


def resolve_pointer(document: JsonValue, pointer: str) -> JsonValue:
    if document is None or (pointer and not pointer.startswith("/")):
        raise InputError("SOURCE_POINTER_INVALID")
    current: JsonValue = document
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, dict):
                current = current[key]
            elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                current = current[int(key)]
            else:
                raise InputError("SOURCE_POINTER_INVALID")
        except (KeyError, IndexError) as exc:
            raise InputError("SOURCE_POINTER_INVALID") from exc
    return current
