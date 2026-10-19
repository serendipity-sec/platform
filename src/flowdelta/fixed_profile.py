"""Opt-in workflow-only comparison gate over the existing 0.4.0 input contract.

Profiles are trusted, separately pinned inputs, never part of a candidate's manifest.
This gate reads declared configuration only; it does not approve runtime execution.
"""

from copy import deepcopy
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from flowdelta.contracts import ArtifactRef, Contract, Handoff, Identifier, NonEmpty, Sha256
from flowdelta.dependencies import dependency_closure
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import (
    InputError,
    LoadedCase,
    _read_local,
    digest,
    json_bytes,
    load_case,
    parse_file_model,
    parse_json,
    parse_model,
)
from flowdelta.normalize import normalize
from flowdelta.snapshots import build_snapshots


class FixedComparisonProfile(Contract):
    """A separately versioned gate; the existing Handoff schema is unchanged."""

    profile_format: Literal["flowdelta-fixed-profile-v1"]
    profile_id: Identifier
    revision: NonEmpty
    source_case_id: Identifier
    family_id: Identifier
    case_contract_sha256: Sha256
    fixed_artifacts: list[ArtifactRef] = Field(min_length=1, max_length=64)
    model_nodes: dict[str, Sha256]
    model_connections_sha256: Sha256
    additional_fixed_nodes: dict[str, Sha256]
    allowed_changed_axes: list[Literal["workflow"]] = Field(min_length=1, max_length=1)
    interpretation: Literal["DECLARED_CONFIGURATION_ONLY"]

    @model_validator(mode="after")
    def unique_artifacts(self) -> Self:
        paths = [ref.path.casefold() for ref in self.fixed_artifacts]
        ids = [ref.artifact_id for ref in self.fixed_artifacts]
        if len(set(paths)) != len(paths) or len(set(ids)) != len(ids):
            raise ValueError("duplicate fixed artifact")
        if any(ref.kind != "source" for ref in self.fixed_artifacts):
            raise ValueError("fixed artifacts must be sources")
        return self


class ProfileValidation(Contract):
    profile_id: Identifier
    revision: NonEmpty
    profile_sha256: Sha256
    case_id: Identifier
    before_snapshot_id: str
    after_snapshot_id: str
    status: Literal["FIXED_PROFILE_MATCH"] = "FIXED_PROFILE_MATCH"
    execution_status: Literal["NOT_RUN"] = "NOT_RUN"
    interpretation: Literal["DECLARED_CONFIGURATION_ONLY"] = "DECLARED_CONFIGURATION_ONLY"


def profile_digest(profile: FixedComparisonProfile) -> str:
    """Canonical document hash, independent of JSON indentation and key order."""
    return digest(json_bytes(profile.model_dump(mode="json")))


def _case_digest(loaded: LoadedCase) -> str:
    # Candidate identity and provenance may change; obligations/roles/extensions may not.
    value = loaded.case.model_dump(mode="json", exclude={"case_id", "provenance_ref"})
    return digest(json_bytes(value))


def _fixed_artifacts(loaded: LoadedCase) -> list[ArtifactRef]:
    changing = {"case.json", loaded.case.after_ref, loaded.case.provenance_ref}
    return sorted(
        (ref for ref in loaded.manifest.artifacts if ref.path not in changing),
        key=lambda ref: ref.path,
    )


def _require_workflow_only(loaded: LoadedCase) -> None:
    if loaded.case.changed_axes != ["workflow"]:
        raise InputError("PROFILE_CHANGED_AXES_REJECTED")
    manifest = loaded.dependency_manifest
    if manifest is None:
        raise InputError("PROFILE_DEPENDENCIES_REQUIRED")
    before = dependency_closure(manifest, "before")
    after = dependency_closure(manifest, "after")
    if set(manifest.before_roots) != set(manifest.after_roots) or before != after:
        raise InputError("PROFILE_DEPENDENCIES_CHANGED")
    if any(snapshot.dependency_limits for snapshot in build_snapshots(loaded).snapshots):
        raise InputError("PROFILE_DEPENDENCIES_UNRESOLVED")


def _node_hash(node: dict[str, JsonValue]) -> str:
    value = deepcopy(node)
    value.pop("position", None)
    return digest(json_bytes(value))


def _dynamic(value: JsonValue) -> bool:
    if isinstance(value, str):
        return value.startswith("=") or "{{" in value
    if isinstance(value, dict):
        return any(_dynamic(child) for child in value.values())
    if isinstance(value, list):
        return any(_dynamic(child) for child in value)
    return False


def _model_configuration(
    document: dict[str, JsonValue], additional_ids: set[str]
) -> tuple[dict[str, str], str, dict[str, str]]:
    workflow = normalize(document, "profile-workflow")
    raw_nodes = document["nodes"]
    assert isinstance(raw_nodes, list)  # normalize checked the workflow structure.
    nodes = {str(node["id"]): node for node in raw_nodes if isinstance(node, dict)}
    if not additional_ids <= nodes.keys():
        raise InputError("PROFILE_FIXED_NODE_MISSING")
    model_ids = {
        node.node_id
        for node in workflow.nodes
        if node.node_type.startswith("@n8n/n8n-nodes-langchain.lm")
    }
    connections = []
    for edge in workflow.connections:
        if "ai_languageModel" in (edge.source_output_type, edge.target_input_type):
            # This is inventory, not a statement about control or execution direction.
            model_ids.add(edge.source_node_id)
            connections.append(edge.model_dump(mode="json", exclude={"source"}))
    for identity in model_ids:
        if _dynamic(nodes[identity].get("parameters", {})):
            raise InputError("PROFILE_DYNAMIC_MODEL_CONFIGURATION")
    return (
        {identity: _node_hash(nodes[identity]) for identity in sorted(model_ids)},
        digest(json_bytes(sorted(connections, key=json_bytes))),
        {identity: _node_hash(nodes[identity]) for identity in sorted(additional_ids)},
    )


def freeze_profile(
    loaded: LoadedCase,
    *,
    profile_id: str,
    revision: str,
    additional_fixed_node_ids: tuple[str, ...] = (),
) -> FixedComparisonProfile:
    """Explicitly establish a baseline; validation never calls this automatically."""
    _require_workflow_only(loaded)
    if len(set(additional_fixed_node_ids)) != len(additional_fixed_node_ids):
        raise InputError("PROFILE_DUPLICATE_FIXED_NODE")
    models, connections, additional = _model_configuration(
        loaded.documents[loaded.case.before_ref], set(additional_fixed_node_ids)
    )
    profile = parse_model(
        FixedComparisonProfile,
        {
            "schema_version": "0.4.0",
            "profile_format": "flowdelta-fixed-profile-v1",
            "profile_id": profile_id,
            "revision": revision,
            "source_case_id": loaded.case.case_id,
            "family_id": loaded.case.family_id,
            "case_contract_sha256": _case_digest(loaded),
            "fixed_artifacts": [ref.model_dump(mode="json") for ref in _fixed_artifacts(loaded)],
            "model_nodes": {key: value for key, value in models.items()},
            "model_connections_sha256": connections,
            "additional_fixed_nodes": {key: value for key, value in additional.items()},
            "allowed_changed_axes": ["workflow"],
            "interpretation": "DECLARED_CONFIGURATION_ONLY",
        },
    )
    validate_profile(loaded, profile)
    return profile


def validate_profile(loaded: LoadedCase, profile: FixedComparisonProfile) -> ProfileValidation:
    """Use a trusted profile; use read_profile at an untrusted file boundary."""
    _require_workflow_only(loaded)
    if (
        loaded.case.family_id != profile.family_id
        or _case_digest(loaded) != profile.case_contract_sha256
    ):
        raise InputError("PROFILE_CASE_CONTRACT_CHANGED")
    if _fixed_artifacts(loaded) != profile.fixed_artifacts:
        raise InputError("PROFILE_FIXED_INPUT_CHANGED")
    for path in (loaded.case.before_ref, loaded.case.after_ref):
        models, connections, additional = _model_configuration(
            loaded.documents[path], set(profile.additional_fixed_nodes)
        )
        if models != profile.model_nodes or connections != profile.model_connections_sha256:
            raise InputError("PROFILE_MODEL_CONFIGURATION_CHANGED")
        if additional != profile.additional_fixed_nodes:
            raise InputError("PROFILE_FIXED_NODE_CHANGED")
    before, after = build_snapshots(loaded).snapshots
    return ProfileValidation(
        profile_id=profile.profile_id,
        revision=profile.revision,
        profile_sha256=profile_digest(profile),
        case_id=loaded.case.case_id,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
    )


def read_profile(path: Path, *, expected_sha256: str) -> FixedComparisonProfile:
    # Reject candidate-controlled replacement profiles, even if their own hashes are valid.
    try:
        root = path.absolute().parent.resolve(strict=True)
    except OSError as exc:
        raise InputError("PROFILE_READ_ERROR") from exc
    profile = parse_file_model(FixedComparisonProfile, parse_json(_read_local(root, path.name)))
    if profile_digest(profile) != expected_sha256:
        raise InputError("PROFILE_IDENTITY_MISMATCH")
    return profile


def write_profile(profile: FixedComparisonProfile, output: Path) -> None:
    try:
        with output.open("xb") as stream:
            stream.write(json_bytes(profile.model_dump(mode="json")))
    except FileExistsError as exc:
        raise InputError("OUTPUT_ALREADY_EXISTS") from exc
    except OSError as exc:
        raise InputError("ARTIFACT_WRITE_ERROR") from exc


def load_profiled_handoff(
    bundle: Path, profile_path: Path, *, expected_sha256: str
) -> tuple[LoadedCase, Handoff, ProfileValidation]:
    """Validate the same captured source bytes that will be passed to A/B/C."""
    profile = read_profile(profile_path, expected_sha256=expected_sha256)
    loaded = load_case(bundle)
    result = validate_profile(loaded, profile)
    return loaded, build_handoff(loaded), result
