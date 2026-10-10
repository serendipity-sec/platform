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
    schema_version: Literal["0.3.0"] = "0.3.0"
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
    schema_version: Literal["0.3.0"]


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
    changed_axes: Annotated[list[Literal["workflow"]], Field(min_length=1, max_length=1)]
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
        return self

    def references(self) -> list[str]:
        return [
            self.before_ref,
            self.after_ref,
            self.task_ref,
            self.evaluation_policy_ref,
            self.environment_ref,
            self.initial_state_ref,
            self.provenance_ref,
        ]


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


class Environment(InputContract):
    environment_id: Identifier
    engine: Literal["n8n"]
    target_version: NonEmpty
    runtime_status: Literal["NOT_VALIDATED"]
    service_bindings: dict[str, JsonValue]


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
    dependency_status: Literal["UNRESOLVED"] = "UNRESOLVED"
    runtime_status: Literal["NOT_VALIDATED"] = "NOT_VALIDATED"
    execution_status: Literal["NOT_RUN"] = "NOT_RUN"


class SnapshotIndex(Contract):
    snapshots: list[Snapshot]


class SemanticsIndex(Contract):
    semantics: list[NodeSemantics]


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
