"""Shared files for A/B/C. These types do not implement analysis or plan selection."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from flowdelta.contracts import (
    ArtifactRef,
    BranchCondition,
    Contract,
    Identifier,
    NonEmpty,
    Sha256,
    SourceLocation,
)


class Authorship(Contract):
    origin: Literal["CONTRACT_EXAMPLE", "MODULE_OUTPUT", "RUNTIME_OBSERVATION"]
    producer: NonEmpty
    note: NonEmpty


class NodeMatch(Contract):
    before_node_id: str | None
    after_node_id: str | None
    method: Literal["EXACT_ID", "STRUCTURAL", "ADDED", "REMOVED", "AMBIGUOUS"]
    evidence: list[SourceLocation] = Field(default_factory=list)


class GraphNode(Contract):
    node_id: NonEmpty
    kind: Literal["WORKFLOW_NODE", "RESOURCE", "PRINCIPAL", "CAPABILITY", "DEPENDENCY"]
    workflow_node_id: NonEmpty | None = None
    source: SourceLocation | None = None


class GraphEdge(Contract):
    edge_id: Identifier
    source_node_id: NonEmpty
    target_node_id: NonEmpty
    relation: Literal[
        "CONTROL", "DATA", "INSTRUCTION_INFLUENCE", "CAPABILITY", "DEPENDENCY_PROTECTION"
    ]
    output_port: Annotated[int, Field(ge=0)]
    input_port: Annotated[int, Field(ge=0)]
    output_type: NonEmpty = "main"
    input_type: NonEmpty = "main"
    condition: BranchCondition | None = None
    source_fields: list[NonEmpty] = Field(default_factory=list)
    target_fields: list[NonEmpty] = Field(default_factory=list)
    adapter_version: NonEmpty | None = None
    evidence: list[SourceLocation]
    basis: Literal["STATIC_POSSIBILITY"]


class SemanticGraph(Contract):
    node_ids: list[NonEmpty]
    edges: list[GraphEdge]
    limitations: list[NonEmpty]
    nodes: list[GraphNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_edges(self) -> Self:
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("duplicate graph node")
        if self.nodes and (
            len(self.nodes) != len(self.node_ids)
            or {item.node_id for item in self.nodes} != set(self.node_ids)
        ):
            raise ValueError("typed graph nodes must account for every node id")
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate graph edge")
        if any(
            edge.source_node_id not in self.node_ids or edge.target_node_id not in self.node_ids
            for edge in self.edges
        ):
            raise ValueError("graph edge references unknown node")
        return self


class Change(Contract):
    change_id: Identifier
    node_id: NonEmpty | None
    kind: Literal[
        "ADDED",
        "REMOVED",
        "PARAMETER_CHANGED",
        "DISPLAY_CHANGED",
        "CONNECTION_CHANGED",
        "UNKNOWN",
        "SETTINGS_CHANGED",
        "TYPE_VERSION_CHANGED",
        "CREDENTIAL_CHANGED",
        "DEPENDENCY_CHANGED",
    ]
    before_value: JsonValue
    after_value: JsonValue
    before_source: SourceLocation | None
    after_source: SourceLocation | None


class ImpactPath(Contract):
    path_id: Identifier
    side: Literal["before", "after"]
    change_ids: list[Identifier]
    edge_ids: list[Identifier]
    task_ids: list[Identifier]
    policy_rule_ids: list[Identifier]
    support: Literal["SUPPORTED", "PARTIAL", "UNKNOWN"]
    basis: Literal["STATIC_POSSIBILITY"] = "STATIC_POSSIBILITY"
    limitations: list[NonEmpty]


class AnalysisBundle(Contract):
    analysis_id: Identifier
    case_id: Identifier
    before_snapshot_id: NonEmpty
    after_snapshot_id: NonEmpty
    authorship: Authorship
    node_matches: list[NodeMatch]
    before_graph: SemanticGraph
    after_graph: SemanticGraph
    changes: list[Change]
    task_ref: ArtifactRef
    policy_ref: ArtifactRef
    obligation_ids: list[Identifier]
    limitations: list[NonEmpty]
    impact_paths: list[ImpactPath] = Field(default_factory=list)
    dependency_graphs: dict[str, SemanticGraph] = Field(default_factory=dict)


class InputValueSchema(Contract):
    value_type: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    pattern: NonEmpty | None = None
    properties: list["InputField"] = Field(default_factory=list)
    items: "InputValueSchema | None" = None
    max_items: Annotated[int, Field(ge=0, le=1000)] = 1000

    @model_validator(mode="after")
    def applicable_constraints(self) -> Self:
        if self.pattern is not None and self.value_type != "string":
            raise ValueError("pattern applies only to string")
        if self.properties and self.value_type != "object":
            raise ValueError("properties apply only to object")
        if (self.value_type == "array") != (self.items is not None):
            raise ValueError("array requires an item schema")
        names = [item.name for item in self.properties]
        if len(names) != len(set(names)):
            raise ValueError("duplicate object property")
        return self


class InputField(InputValueSchema):
    name: Identifier
    required: bool


class MaterializedInputBinding(Contract):
    input_name: Identifier
    node_id: NonEmpty
    parameter_pointer: NonEmpty


class Template(Contract):
    template_id: Identifier
    version: NonEmpty
    task_id: Identifier
    runtime_profile: Identifier
    mode: Literal["NORMAL_BUSINESS_REQUEST", "NORMAL_MANUAL_WORKFLOW"]
    method: Literal["GET", "POST"] | None
    webhook_path: NonEmpty | None
    materialized_input_bindings: list[MaterializedInputBinding] = Field(default_factory=list)
    input_fields: list[InputField]
    required_artifact_kinds: list[Identifier]
    allowed_effects: list[NonEmpty]
    timeout_seconds: Annotated[int, Field(gt=0, le=300)]

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        names = [item.name for item in self.input_fields]
        if len(names) != len(set(names)):
            raise ValueError("duplicate template field")
        if self.mode == "NORMAL_MANUAL_WORKFLOW":
            if self.method is not None or self.webhook_path is not None:
                raise ValueError("manual workflow has no HTTP method or path")
            bound = [binding.input_name for binding in self.materialized_input_bindings]
            if len(bound) != len(set(bound)) or set(bound) != set(names):
                raise ValueError("manual workflow requires one binding per input")
        elif self.method is None or self.webhook_path is None or self.materialized_input_bindings:
            raise ValueError("HTTP request requires method and path without manual bindings")
        return self


class TemplateCatalog(Contract):
    catalog_id: Identifier
    authorship: Authorship
    templates: Annotated[list[Template], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_templates(self) -> Self:
        keys = [(item.template_id, item.version) for item in self.templates]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate template version")
        return self


class ObligationBinding(Contract):
    obligation_id: Identifier
    required_artifact_kinds: list[Identifier]


class TestPlan(Contract):
    plan_id: Identifier
    case_id: Identifier
    analysis_id: Identifier
    authorship: Authorship
    retrieval_ref: ArtifactRef | None = None
    planner_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    before_snapshot_id: NonEmpty
    after_snapshot_id: NonEmpty
    catalog_id: Identifier
    template_id: Identifier
    template_version: NonEmpty
    normal_input: dict[str, JsonValue]
    initial_state_ref: ArtifactRef
    obligation_bindings: list[ObligationBinding]
    uncovered_obligation_ids: list[Identifier]
    attempts_per_side: Annotated[int, Field(ge=1, le=10)]
    timeout_seconds: Annotated[int, Field(gt=0, le=300)]

    @model_validator(mode="after")
    def unique_obligations(self) -> Self:
        bound = [item.obligation_id for item in self.obligation_bindings]
        ids = [*bound, *self.uncovered_obligation_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("obligation duplicated between bound/uncovered lists")
        return self


class EvidenceEvent(Contract):
    run_id: Identifier
    producer: NonEmpty
    sequence: Annotated[int, Field(gt=0)]
    observed_at: NonEmpty
    event_type: NonEmpty
    principal: NonEmpty
    tenant: NonEmpty
    resource_id: NonEmpty
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] | None
    outcome: NonEmpty
    response_sha256: Sha256
    attempt_number: Annotated[int, Field(ge=1)] = 1
    execution_id: NonEmpty | None = None
    node_id: NonEmpty | None = None
    node_run_index: Annotated[int, Field(ge=0)] | None = None
    item_index: Annotated[int, Field(ge=0)] | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ItemLink(Contract):
    input_index: Annotated[int, Field(ge=0)]
    item_index: Annotated[int, Field(ge=0)]


class ObservedItem(Contract):
    item_index: Annotated[int, Field(ge=0)]
    json_data: dict[str, JsonValue]
    paired_items: list[ItemLink]
    linkage_status: Literal["OBSERVED", "ABSENT", "UNSUPPORTED"]
    raw_metadata: dict[str, JsonValue] = Field(default_factory=dict)


class NodeObservation(Contract):
    execution_id: NonEmpty
    workflow_id: NonEmpty
    node_id: NonEmpty
    node_name: NonEmpty
    node_run_index: Annotated[int, Field(ge=0)]
    output_type: NonEmpty
    output_index: Annotated[int, Field(ge=0)]
    items: list[ObservedItem]
    execution_status: Literal["SUCCESS", "ERROR", "UNKNOWN"]
    source: SourceLocation


class ArtifactCollection(Contract):
    run_id: Identifier
    artifacts: list[ArtifactRef]
    artifact_kinds: dict[str, Identifier]
    required_kinds: list[Identifier]
    observed_kinds: list[Identifier]
    missing_kinds: list[Identifier]
    collection_status: Literal["COMPLETE", "PARTIAL", "NOT_STARTED"]

    @model_validator(mode="after")
    def account_for_missing(self) -> Self:
        ids = [item.artifact_id for item in self.artifacts]
        paths = [item.path for item in self.artifacts]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ValueError("duplicate artifact identity or path")
        if set(self.observed_kinds) != set(self.artifact_kinds):
            raise ValueError("observed kinds must have file bindings")
        if not set(self.artifact_kinds.values()).issubset(ids):
            raise ValueError("artifact kind references missing artifact")
        expected = set(self.required_kinds) - set(self.observed_kinds)
        if expected != set(self.missing_kinds):
            raise ValueError("missing artifact kinds do not match observed coverage")
        if self.collection_status == "COMPLETE" and expected:
            raise ValueError("cannot complete with missing artifacts")
        return self


class ScenarioCard(Contract):
    card_id: Identifier
    version: NonEmpty
    title: NonEmpty
    summary: NonEmpty
    source: NonEmpty
    reuse_status: Literal["TEAM_AUTHORED", "LICENSED_PUBLIC", "CONSENTED"]
    source_ref: ArtifactRef | None = None
    source_revision: NonEmpty | None = None
    license_identifier: NonEmpty | None = None
    claim_basis: Literal["TEAM_HYPOTHESIS", "AUTHOR_DESCRIPTION", "EXPERIMENTALLY_OBSERVED"] = (
        "TEAM_HYPOTHESIS"
    )
    authorship: Authorship
    runtime_profile: Identifier
    node_types: list[NonEmpty]
    template_id: Identifier
    template_version: NonEmpty
    required_artifact_kinds: list[Identifier]

    @model_validator(mode="after")
    def reusable_source(self) -> Self:
        if self.reuse_status != "TEAM_AUTHORED" and (
            not self.source_ref or not self.source_revision or not self.license_identifier
        ):
            raise ValueError("external card requires source bytes, revision and reuse basis")
        return self


class RetrievalHit(Contract):
    card_id: Identifier
    card_version: NonEmpty
    rank: Annotated[int, Field(ge=1)]
    score: float
    applicability: Literal["MATCH", "PARTIAL", "NO_MATCH"]
    reason: NonEmpty


class RetrievalResult(Contract):
    retrieval_id: Identifier
    analysis_id: Identifier
    authorship: Authorship
    kb_snapshot_ref: ArtifactRef
    method: Literal["KEYWORD", "VECTOR", "HYBRID"]
    model_version: NonEmpty | None
    query: dict[str, JsonValue]
    hits: list[RetrievalHit]
    excluded_reasons: dict[str, NonEmpty]


class ExecutionRun(Contract):
    run_id: Identifier
    comparison_run_id: Identifier
    case_id: Identifier
    plan_id: Identifier
    side: Literal["before", "after"]
    attempt_number: Annotated[int, Field(ge=1)] = 1
    snapshot_id: NonEmpty
    authorship: Authorship
    runtime_profile: Identifier
    actual_engine_version: NonEmpty | None
    actual_image_digest: NonEmpty | None
    initial_state_sha256: Sha256
    phase: Literal[
        "QUEUED", "PREPARING", "READY", "RUNNING", "COLLECTING", "FINALIZING", "FINISHED"
    ]
    execution_status: Literal[
        "NOT_RUN", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"
    ]
    cleanup_status: Literal["PENDING", "COMPLETE", "FAILED"]
    started_at: NonEmpty | None
    finished_at: NonEmpty | None
    error_code: Identifier | None
    outcome: None = None  # Business execution status cannot become a security verdict.
    artifacts: ArtifactCollection

    @model_validator(mode="after")
    def state_consistency(self) -> Self:
        if self.run_id != self.artifacts.run_id:
            raise ValueError("run/artifact identity mismatch")
        if self.execution_status == "SUCCEEDED":
            if (
                not self.actual_engine_version
                or not self.actual_image_digest
                or not self.started_at
            ):
                raise ValueError("successful execution requires actual runtime and start time")
        if self.phase == "FINISHED" and not self.finished_at:
            raise ValueError("finished phase requires finish time")
        return self


class PipelineExamples(Contract):
    analysis: AnalysisBundle
    catalog: TemplateCatalog
    plan: TestPlan
    run: ExecutionRun
    cards: list[ScenarioCard]
    events: list[EvidenceEvent] = Field(default_factory=list)
    observations: list[NodeObservation] = Field(default_factory=list)
    retrieval: RetrievalResult | None = None
