"""Read typed handoffs and check references between separately implemented modules."""

import re
from collections import Counter
from pathlib import Path

from pydantic import JsonValue

from flowdelta.contracts import (
    ArtifactRef,
    ComparisonCase,
    DependencyManifest,
    DependencyWorkflowsIndex,
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
)
from flowdelta.ingestion import (
    parse_file_model as parse_model,
)
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    InputField,
    InputValueSchema,
    PipelineExamples,
    TemplateCatalog,
    TestPlan,
)


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
            "dependencies.normalized.json",
        }
        dependencies = None
        if case.dependency_manifest_ref:
            from flowdelta.dependencies import validate_manifest

            dependencies = parse_model(DependencyManifest, docs[case.dependency_manifest_ref])
            validate_manifest(dependencies)
            expected.update(item.path for item in dependencies.dependencies)
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
            dependency_manifest=dependencies,
            dependency_workflows=parse_model(
                DependencyWorkflowsIndex, docs["dependencies.normalized.json"]
            ).workflows,
            dependency_semantics=parse_model(
                DependencyWorkflowsIndex, docs["dependencies.normalized.json"]
            ).semantics,
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
        for dependency in snapshot.dependencies:
            if refs.get(dependency.artifact_ref.path) != dependency.artifact_ref:
                raise InputError("DEPENDENCY_SOURCE_MISMATCH")
    if result.policy.bindings is not None:
        from flowdelta.normalize import normalize
        from flowdelta.policy_bindings import resolve_policy_bindings, validate_policy_bindings

        raw_before = normalize(docs[case.before_ref], refs[case.before_ref].artifact_id)
        raw_after = normalize(docs[case.after_ref], refs[case.after_ref].artifact_id)
        validate_policy_bindings(result.policy, raw_before, raw_after)
        # Pointer validation must remain tied to original bytes after a handoff round trip.
        for raw, normalized in [(raw_before, result.before), (raw_after, result.after)]:

            def locations(workflow: NormalizedWorkflow) -> list[tuple[object, ...]]:
                return [(node.node_id, node.source, node.parameters) for node in workflow.nodes]

            _same(locations(raw), locations(normalized), "POLICY_BINDING_SOURCE_MISMATCH")
        resolve_policy_bindings(result)
    return result


def verify_ref(root: Path, ref: ArtifactRef) -> None:
    data = _read_local(root.resolve(strict=True), ref.path)
    if len(data) != ref.size_bytes or digest(data) != ref.sha256:
        raise InputError("ARTIFACT_INTEGRITY_MISMATCH")


def _same(actual: object, expected: object, code: str) -> None:
    if actual != expected:
        raise InputError(code)


def validate_input_value(value: JsonValue, schema: InputValueSchema, depth: int = 0) -> None:
    if depth > 16:
        raise InputError("TEMPLATE_INPUT_DEPTH_LIMIT")
    expected_types = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
        "null": (type(None),),
    }
    if type(value) not in expected_types[schema.value_type]:
        raise InputError("TEMPLATE_INPUT_INVALID")
    if isinstance(value, str) and schema.pattern is not None:
        try:
            matched = re.fullmatch(schema.pattern, value)
        except re.error as exc:
            raise InputError("TEMPLATE_PATTERN_INVALID") from exc
        if matched is None:
            raise InputError("TEMPLATE_INPUT_INVALID")
    elif isinstance(value, list):
        if schema.items is None or len(value) > schema.max_items:
            raise InputError("TEMPLATE_INPUT_INVALID")
        for item in value:
            validate_input_value(item, schema.items, depth + 1)
    elif isinstance(value, dict):
        validate_input_fields(value, schema.properties, depth + 1)


def validate_input_fields(
    value: dict[str, JsonValue], fields: list[InputField], depth: int = 0
) -> None:
    by_name = {item.name: item for item in fields}
    if (
        len(by_name) != len(fields)
        or set(value) - set(by_name)
        or any(item.required and item.name not in value for item in fields)
    ):
        raise InputError("TEMPLATE_INPUT_FIELDS_MISMATCH")
    for name, item in value.items():
        validate_input_value(item, by_name[name], depth)


def validate_pipeline(
    handoff: Handoff,
    analysis: AnalysisBundle,
    catalog: TemplateCatalog,
    plan: TestPlan,
) -> None:
    """Cross-file contracts, independent of A/B's future generation algorithms."""
    _validate_pipeline_context(handoff, analysis, plan, catalog.catalog_id)
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
    validate_input_fields(plan.normal_input, template.input_fields)
    _same(plan.normal_input, handoff.task.normal_input, "NORMAL_INPUT_MISMATCH")
    from flowdelta.order_scenarios import scenario_ref

    if template.mode == "REGISTERED_SECURITY_REQUEST":
        if scenario_ref(plan) is None:
            raise InputError("ORDER_SCENARIO_REQUIRED")
    elif scenario_ref(plan) is not None:
        raise InputError("SCENARIO_REQUIRES_SECURITY_TEMPLATE")
    if template.mode == "NORMAL_MANUAL_WORKFLOW":
        for workflow in [handoff.before, handoff.after]:
            nodes = {node.node_id: node for node in workflow.nodes}
            for input_binding in template.materialized_input_bindings:
                if input_binding.node_id not in nodes:
                    raise InputError("MANUAL_INPUT_NODE_NOT_FOUND")
                _same(
                    resolve_pointer(
                        nodes[input_binding.node_id].parameters, input_binding.parameter_pointer
                    ),
                    plan.normal_input[input_binding.input_name],
                    "MANUAL_INPUT_BINDING_MISMATCH",
                )
    for binding in plan.obligation_bindings:
        if not set(binding.required_artifact_kinds).issubset(template.required_artifact_kinds):
            raise InputError("TEMPLATE_ARTIFACT_MISSING")


def _validate_pipeline_context(
    handoff: Handoff,
    analysis: AnalysisBundle,
    plan: TestPlan,
    catalog_id: str,
) -> None:
    """Validate shared case, reference, obligation, and graph links for any plan catalog."""
    from flowdelta.policy_bindings import resolve_policy_bindings

    resolve_policy_bindings(handoff)
    policy_ids = {rule.rule_id for rule in handoff.policy.rules}
    for impact in analysis.impact_paths:
        if not set(impact.policy_rule_ids).issubset(policy_ids):
            raise InputError("IMPACT_PATH_POLICY_UNKNOWN")
        if not set(impact.task_ids).issubset({handoff.task.task_id}):
            raise InputError("IMPACT_PATH_TASK_UNKNOWN")
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
    _same(plan.catalog_id, catalog_id, "CATALOG_ID_MISMATCH")
    by_path = {ref.path: ref for ref in handoff.source_manifest.artifacts}
    for actual, path in [
        (analysis.task_ref, case.task_ref),
        (analysis.policy_ref, case.evaluation_policy_ref),
        (plan.initial_state_ref, case.initial_state_ref),
    ]:
        expected = by_path[path].model_copy(update={"path": f"input/{path}"})
        _same(actual, expected, "CONTEXT_REF_MISMATCH")
    required = {item.obligation_id: item for item in case.obligations}
    _same(set(analysis.obligation_ids), set(required), "ANALYSIS_OBLIGATION_MISMATCH")
    bound = {item.obligation_id: item for item in plan.obligation_bindings}
    covered = set(bound) | set(plan.uncovered_obligation_ids)
    _same(covered, set(required), "REQUIRED_OBLIGATION_MISSING")
    for obligation_id, binding in bound.items():
        if not set(required[obligation_id].required_artifacts).issubset(
            binding.required_artifact_kinds
        ):
            raise InputError("OBLIGATION_ARTIFACT_MISSING")
    for side, graph, workflow in [
        ("before", analysis.before_graph, handoff.before),
        ("after", analysis.after_graph, handoff.after),
    ]:
        workflow_ids = (
            {item.workflow_node_id for item in graph.nodes if item.kind == "WORKFLOW_NODE"}
            if graph.nodes
            else set(graph.node_ids)
        )
        _same(workflow_ids, {node.node_id for node in workflow.nodes}, "GRAPH_NODE_MISMATCH")
        logical_ids = (
            {item.node_id: item.workflow_node_id for item in graph.nodes}
            if graph.nodes
            else {key: key for key in graph.node_ids}
        )
        connections = {
            (
                edge.source_node_id,
                edge.target_node_id,
                edge.source_output_type,
                edge.source_output_index,
                edge.target_input_type,
                edge.target_input_index,
            )
            for edge in workflow.connections
        }
        facts = {item.node_id: item for item in handoff.semantics.semantics if item.side == side}
        for edge in graph.edges:
            if edge.relation != "CONTROL":
                continue
            source_id, target_id = (
                logical_ids[edge.source_node_id],
                logical_ids[edge.target_node_id],
            )
            if (
                source_id,
                target_id,
                edge.output_type,
                edge.output_port,
                edge.input_type,
                edge.input_port,
            ) not in connections:
                raise InputError("GRAPH_CONTROL_PORT_MISMATCH")
            source_facts = facts.get(source_id) if source_id is not None else None
            if source_facts and source_facts.branches:
                matches = [
                    branch
                    for branch in source_facts.branches
                    if (branch.output_type, branch.output_index)
                    == (edge.output_type, edge.output_port)
                ]
                if matches and edge.condition != matches[0]:
                    raise InputError("GRAPH_BRANCH_CONDITION_MISMATCH")
        actual_controls = Counter(
            (
                logical_ids[edge.source_node_id],
                logical_ids[edge.target_node_id],
                edge.output_type,
                edge.output_port,
                edge.input_type,
                edge.input_port,
            )
            for edge in graph.edges
            if edge.relation == "CONTROL"
        )
        expected_controls = Counter(
            (
                edge.source_node_id,
                edge.target_node_id,
                edge.source_output_type,
                edge.source_output_index,
                edge.target_input_type,
                edge.target_input_index,
            )
            for edge in workflow.connections
        )
        if actual_controls != expected_controls:
            raise InputError("GRAPH_CONTROL_COVERAGE_MISMATCH")
        for impact_path in analysis.impact_paths:
            if impact_path.side == side and (
                not set(impact_path.edge_ids).issubset({edge.edge_id for edge in graph.edges})
                or not set(impact_path.change_ids).issubset(
                    {change.change_id for change in analysis.changes}
                )
            ):
                raise InputError("IMPACT_PATH_REFERENCE_MISMATCH")


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
