"""Input and structural handoff contracts, intentionally separate from runtime support."""

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1, max_length=4096)]


def validate_relative_path(value: str) -> str:
    """Use one portable path format on Windows and Linux."""
    if len(value) > 512 or not value:
        raise ValueError("invalid relative path")
    for part in value.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '\\<>:"|?*' for char in part)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", part)
        ):
            raise ValueError("invalid relative path")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    schema_version: Literal["0.4.0"] = "0.4.0"
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("extensions")
    @classmethod
    def namespaced_extensions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*", key) for key in value):
            raise ValueError("extensions require namespace.name keys")
        return value


class ArtifactRef(Contract):
    artifact_id: Identifier
    path: str
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, le=8 * 1024 * 1024)]
    media_type: Literal["application/json", "text/plain"] = "application/json"
    kind: Literal["source", "generated"]

    _path = field_validator("path")(validate_relative_path)


class InputContract(Contract):
    schema_version: Literal["0.4.0"]


class Manifest(InputContract):
    artifacts: Annotated[list[ArtifactRef], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_artifacts(self) -> Self:
        for values in (
            [item.artifact_id for item in self.artifacts],
            [item.path.casefold() for item in self.artifacts],
        ):
            if len(set(values)) != len(values):
                raise ValueError("duplicate artifact id or path")
        if any(item.path == "manifest.json" for item in self.artifacts):
            raise ValueError("manifest cannot reference itself")
        return self


class VerificationObligation(Contract):
    obligation_id: Identifier
    required: bool
    description: NonEmpty
    required_artifacts: list[Identifier]


class ComparisonCase(InputContract):
    case_id: Identifier
    family_id: Identifier
    before_ref: str
    after_ref: str
    task_ref: str
    evaluation_policy_ref: str
    environment_ref: str
    initial_state_ref: str
    provenance_ref: str
    dependency_manifest_ref: str | None = None
    changed_axes: Annotated[
        list[Literal["workflow", "dependencies"]], Field(min_length=1, max_length=2)
    ]
    baseline_security_status: Literal["UNKNOWN"]
    obligations: list[VerificationObligation]

    _refs = field_validator(
        "before_ref",
        "after_ref",
        "task_ref",
        "evaluation_policy_ref",
        "environment_ref",
        "initial_state_ref",
        "provenance_ref",
    )(validate_relative_path)

    @model_validator(mode="after")
    def distinct_refs(self) -> Self:
        refs = self.references()
        if len(set(refs)) != len(refs):
            raise ValueError("case roles must reference distinct files")
        ids = [item.obligation_id for item in self.obligations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate obligation id")
        if len(set(self.changed_axes)) != len(self.changed_axes):
            raise ValueError("duplicate changed axis")
        return self

    def references(self) -> list[str]:
        refs = [
            self.before_ref,
            self.after_ref,
            self.task_ref,
            self.evaluation_policy_ref,
            self.environment_ref,
            self.initial_state_ref,
            self.provenance_ref,
        ]
        if self.dependency_manifest_ref is not None:
            refs.append(validate_relative_path(self.dependency_manifest_ref))
        return refs


class DependencySpec(Contract):
    dependency_id: Identifier
    kind: Literal["WORKFLOW", "MCP_DESCRIPTOR", "SKILL_DESCRIPTOR", "RUNTIME_CONFIG"]
    version: NonEmpty
    path: str
    workflow_id: NonEmpty | None = None
    requires: list[Identifier] = Field(default_factory=list)
    _path = field_validator("path")(validate_relative_path)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.kind == "WORKFLOW") != (self.workflow_id is not None):
            raise ValueError("workflow dependency requires exactly one fixed workflow id")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("duplicate dependency reference")
        return self


class DependencyManifest(InputContract):
    dependencies: Annotated[list[DependencySpec], Field(max_length=32)]
    before_roots: list[Identifier]
    after_roots: list[Identifier]

    @model_validator(mode="after")
    def unique_dependencies(self) -> Self:
        ids = [item.dependency_id for item in self.dependencies]
        paths = [item.path.casefold() for item in self.dependencies]
        if any(len(set(values)) != len(values) for values in [ids, paths]):
            raise ValueError("duplicate dependency identity")
        for roots in [self.before_roots, self.after_roots]:
            if len(set(roots)) != len(roots):
                raise ValueError("duplicate dependency root")
        return self


class ResolvedDependency(Contract):
    dependency_id: Identifier
    kind: Literal["WORKFLOW", "MCP_DESCRIPTOR", "SKILL_DESCRIPTOR", "RUNTIME_CONFIG"]
    version: NonEmpty
    artifact_ref: ArtifactRef
    workflow_id: NonEmpty | None = None
    requires: list[Identifier]
    interpretation: Literal["DECLARED_LOCAL_FILE"] = "DECLARED_LOCAL_FILE"


class TaskContract(InputContract):
    task_id: Identifier
    purpose: NonEmpty
    actor: NonEmpty
    normal_input: dict[str, JsonValue]
    success_conditions: Annotated[list[NonEmpty], Field(min_length=1)]
    protected_resources: list[NonEmpty]
    allowed_side_effects: list[NonEmpty]
    provenance_ref: str
    _ref = field_validator("provenance_ref")(validate_relative_path)


class PolicyRule(Contract):
    rule_id: Identifier
    subject: NonEmpty
    resource: NonEmpty
    condition: NonEmpty
    exceptions: list[NonEmpty]
    rationale: NonEmpty


class Policy(InputContract):
    policy_id: Identifier
    policy_version: NonEmpty
    interpretation: Literal["DECLARED_ONLY"]
    rules: Annotated[list[PolicyRule], Field(min_length=1)]
    provenance_ref: str
    _ref = field_validator("provenance_ref")(validate_relative_path)

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        ids = [rule.rule_id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate policy rule id")
        return self


class HttpEndpoint(Contract):
    endpoint_id: Identifier
    method: Literal["GET", "POST"]
    url: NonEmpty
    response_fields: list[NonEmpty]
    response_shape: Literal["SINGLE_JSON_OBJECT", "JSON_ITEM_ARRAY"]
    description_basis: Literal["DECLARED_ONLY"] = "DECLARED_ONLY"


class Environment(InputContract):
    environment_id: Identifier
    engine: Literal["n8n"]
    target_version: NonEmpty
    runtime_status: Literal["NOT_VALIDATED"]
    service_bindings: dict[str, JsonValue]
    http_endpoints: list[HttpEndpoint] = Field(default_factory=list)


class InitialState(InputContract):
    state_id: Identifier
    data: dict[str, JsonValue]


class Provenance(InputContract):
    provenance_id: Identifier
    source_kind: Literal["TEAM_SYNTHETIC", "PUBLIC_ADAPTED", "CONSENTED_SANITIZED"]
    source: NonEmpty
    reuse_status: NonEmpty
    transformations: list[NonEmpty]
    production_equivalence: Literal["NOT_CLAIMED"]


class SourceLocation(Contract):
    artifact_id: Identifier
    json_pointer: str


class ValueExpression(Contract):
    """Restricted syntax tree. It is data and is never passed to eval()."""

    kind: Literal["constant", "current_field", "named_field", "object", "concat", "compare"]
    value: JsonValue = None
    field_path: list[NonEmpty] = Field(default_factory=list)
    node_name: NonEmpty | None = None
    item_selection: Literal["CURRENT", "LINKED", "FIRST"] = "CURRENT"
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte"] | None = None
    terms: list["ValueExpression"] = Field(default_factory=list)
    members: dict[str, "ValueExpression"] = Field(default_factory=dict)
    comparison_type: Literal["string", "number", "boolean"] | None = None

    @model_validator(mode="after")
    def expression_shape(self) -> Self:
        if self.kind == "named_field" and (not self.node_name or self.item_selection == "CURRENT"):
            raise ValueError("named reference requires explicit node and selection")
        if self.kind != "named_field" and self.node_name is not None:
            raise ValueError("node name applies only to named reference")
        if self.kind == "compare" and (len(self.terms) != 2 or self.operator is None):
            raise ValueError("comparison requires two operands and an operator")
        if self.kind != "compare" and (
            self.operator is not None or self.comparison_type is not None
        ):
            raise ValueError("comparison metadata on non-comparison")
        if self.kind == "concat" and len(self.terms) != 2:
            raise ValueError("concatenation requires two operands")
        if self.kind not in {"compare", "concat"} and self.terms:
            raise ValueError("unexpected expression operands")
        if self.kind != "object" and self.members:
            raise ValueError("members apply only to object expression")
        return self


class FieldAssignment(Contract):
    field_name: NonEmpty
    value_type: Literal["string", "number", "boolean", "object", "array"]
    value: ValueExpression
    source: SourceLocation
    conversion: Literal["N8N_STANDARD"] = "N8N_STANDARD"


class BranchCondition(Contract):
    output_type: NonEmpty = "main"
    output_index: Annotated[int, Field(ge=0)]
    predicate: ValueExpression
    when: bool
    source: SourceLocation


class ItemBehavior(Contract):
    processing: Literal[
        "SINGLE_REQUEST", "PER_ITEM", "FILTER", "APPEND", "SPLIT_ARRAY", "SUBWORKFLOW", "UNKNOWN"
    ]
    cardinality: Literal["ONE", "ONE_PER_INPUT", "SUBSET", "SUM_INPUTS", "VARIABLE", "UNKNOWN"]
    lineage: Literal["SOURCE", "PRESERVE", "INPUT_INDEX", "CALLER", "UNKNOWN"]
    input_ports: Annotated[int, Field(ge=0)]
    output_ports: Annotated[int, Field(ge=0)]
    note: NonEmpty
    expression_item_scope: Literal["CURRENT", "FIRST", "ALL", "CONTEXT_DEPENDENT"] = "CURRENT"


class ErrorBehavior(Contract):
    on_error: Literal["stopWorkflow", "continueRegularOutput", "continueErrorOutput"]
    retry_on_fail: bool = False
    max_tries: Annotated[int, Field(ge=1, le=5)] = 1
    wait_between_tries_ms: Annotated[int, Field(ge=0, le=5000)] = 0
    always_output_data: bool = False
    execute_once: bool = False
    error_output_index: Annotated[int, Field(ge=0)] | None = None


class NormalizedNode(Contract):
    node_id: NonEmpty
    name: NonEmpty
    node_type: NonEmpty
    type_version: Annotated[int | float, Field(gt=0)]
    parameters: dict[str, JsonValue]
    # Unknown n8n fields are preserved; they do not enter the strict contract namespace.
    opaque_fields: dict[str, JsonValue]
    source: SourceLocation
    semantic_support: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"] = "UNKNOWN"


class Connection(Contract):
    source_node_id: str
    source_output_type: str
    source_output_index: Annotated[int, Field(ge=0)]
    target_node_id: str
    target_input_type: str
    target_input_index: Annotated[int, Field(ge=0)]
    opaque_fields: dict[str, JsonValue]
    source: SourceLocation


class NormalizedWorkflow(Contract):
    source_artifact_id: Identifier
    workflow_id: str | None
    normalizer_version: Literal["structure-v1"] = "structure-v1"
    nodes: list[NormalizedNode]
    connections: list[Connection]
    opaque_fields: dict[str, JsonValue]
    # Empty output ports and original ordering remain available here, too.
    original_connections: dict[str, JsonValue]
    processing_status: Literal["STRUCTURE_PARSED"] = "STRUCTURE_PARSED"
    semantic_support: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNKNOWN"] = "UNKNOWN"
    execution_status: Literal["NOT_RUN"] = "NOT_RUN"
    limitations: list[str]


class NodeSemantics(Contract):
    node_id: str
    side: Literal["before", "after"]
    node_type: str
    type_version: int | float
    operation: JsonValue
    reads: list[str] | None = None
    writes: list[str] | None = None
    effects: list[str] | None = None
    support: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"] = "UNKNOWN"
    reason: str = "NO_PROFILE_SELECTED"
    adapter_version: str | None = None
    engine_version: str | None = None
    capabilities: list[str] | None = None
    parameter_sources: list[SourceLocation] = Field(default_factory=list)
    expressions: dict[str, ValueExpression] = Field(default_factory=dict)
    assignments: list[FieldAssignment] = Field(default_factory=list)
    branches: list[BranchCondition] = Field(default_factory=list)
    item_behavior: ItemBehavior | None = None
    error_behavior: ErrorBehavior | None = None
    dependency_ids: list[Identifier] = Field(default_factory=list)
    unsupported_locations: list[SourceLocation] = Field(default_factory=list)
    resolved_defaults: dict[str, JsonValue] = Field(default_factory=dict)
    source: SourceLocation


class Snapshot(Contract):
    snapshot_id: str
    case_id: Identifier
    side: Literal["before", "after"]
    workflow_ref: ArtifactRef
    context_refs: list[ArtifactRef]
    raw_sha256: Sha256
    structural_sha256: Sha256
    semantic_sha256: Sha256 | None = None
    semantic_hash_version: Literal["config-v1"] | None = None
    semantic_hash_scope: Literal["DECLARED_CONFIGURATION"] | None = None
    dependency_status: Literal["UNRESOLVED", "DECLARED_RESOLVED"] = "UNRESOLVED"
    dependencies: list[ResolvedDependency] = Field(default_factory=list)
    dependency_limits: list[NonEmpty] = Field(default_factory=list)
    runtime_status: Literal["NOT_VALIDATED"] = "NOT_VALIDATED"
    execution_status: Literal["NOT_RUN"] = "NOT_RUN"

    @model_validator(mode="after")
    def hash_scope_is_explicit(self) -> Self:
        if self.raw_sha256 != self.workflow_ref.sha256:
            raise ValueError("raw workflow hash mismatch")
        if self.semantic_sha256 is not None:
            if (
                self.semantic_hash_version is None
                or self.semantic_hash_scope is None
                or self.dependency_status != "DECLARED_RESOLVED"
                or self.dependency_limits
            ):
                raise ValueError("configuration hash requires resolved declared scope")
        elif self.semantic_hash_version is not None or self.semantic_hash_scope is not None:
            raise ValueError("hash metadata requires a hash")
        return self


class SnapshotIndex(Contract):
    snapshots: list[Snapshot]


class SemanticsIndex(Contract):
    semantics: list[NodeSemantics]


class DependencyWorkflowsIndex(Contract):
    workflows: dict[str, NormalizedWorkflow]
    semantics: dict[str, SemanticsIndex] = Field(default_factory=dict)


class Handoff(Contract):
    """Schema root; the CLI writes these members to documented individual files."""

    case: ComparisonCase
    task: TaskContract
    policy: Policy
    environment: Environment
    initial_state: InitialState
    provenance: Provenance
    source_manifest: Manifest
    snapshots: SnapshotIndex
    before: NormalizedWorkflow
    after: NormalizedWorkflow
    semantics: SemanticsIndex
    dependency_manifest: DependencyManifest | None = None
    dependency_workflows: dict[str, NormalizedWorkflow] = Field(default_factory=dict)
    dependency_semantics: dict[str, SemanticsIndex] = Field(default_factory=dict)
