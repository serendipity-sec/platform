"""Produce an immutable local handoff from the exact bytes validated by the loader."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from flowdelta.artifact_io import write_new_bytes
from flowdelta.contracts import (
    ArtifactRef,
    DependencyWorkflowsIndex,
    Handoff,
    Manifest,
    SemanticsIndex,
)
from flowdelta.ingestion import MAX_FILE_BYTES, InputError, LoadedCase, digest, json_bytes
from flowdelta.normalize import describe_nodes, normalize
from flowdelta.policy_bindings import validate_policy_bindings
from flowdelta.semantics import PROFILE, describe_profile
from flowdelta.snapshots import build_snapshots

GENERATED_PATHS = {
    "before.normalized.json",
    "after.normalized.json",
    "semantics.json",
    "snapshots.json",
    "source-manifest.json",
    "dependencies.normalized.json",
    "manifest.json",
}


def build_handoff(loaded: LoadedCase) -> Handoff:
    case = loaded.case
    by_path = {ref.path: ref for ref in loaded.manifest.artifacts}
    before = normalize(loaded.documents[case.before_ref], by_path[case.before_ref].artifact_id)
    after = normalize(loaded.documents[case.after_ref], by_path[case.after_ref].artifact_id)
    validate_policy_bindings(loaded.policy, before, after)
    semantics = [*describe_nodes(before, "before"), *describe_nodes(after, "after")]
    if loaded.environment.extensions.get("flowdelta.semantic_profile") == PROFILE:
        semantics = []
        for side, workflow in [("before", before), ("after", after)]:
            typed_semantic_side: Literal["before", "after"] = (
                "before" if side == "before" else "after"
            )
            described = describe_profile(
                workflow, typed_semantic_side, loaded.environment.target_version
            )
            semantics.extend(described)
            for node, description in zip(workflow.nodes, described, strict=True):
                node.semantic_support = description.support
            statuses = {item.support for item in described}
            workflow.semantic_support = (
                "SUPPORTED"
                if statuses == {"SUPPORTED"}
                else "UNSUPPORTED"
                if statuses == {"UNSUPPORTED"}
                else "PARTIAL"
            )
            workflow.limitations = [
                "Semantic adapter covers only the registered normal order-lookup profile.",
                "Effects are static possibilities; observations live in ExecutionRun artifacts.",
                "Configuration hashes cover declared local dependencies, not behavior equivalence.",
            ]
    if loaded.environment.extensions.get("flowdelta.semantic_profile") == "core-local-v1":
        from flowdelta.core_adapters import AdapterContext, describe_core

        semantics = []
        for typed_core_side, workflow in [("before", before), ("after", after)]:
            core_side: Literal["before", "after"] = (
                "before" if typed_core_side == "before" else "after"
            )
            descriptions = describe_core(
                workflow,
                AdapterContext(
                    loaded.environment,
                    workflow,
                    core_side,
                    loaded.dependency_manifest,
                ),
            )
            semantics.extend(descriptions)
            for node, description in zip(workflow.nodes, descriptions, strict=True):
                node.semantic_support = description.support
            statuses = {item.support for item in descriptions}
            workflow.semantic_support = (
                "SUPPORTED"
                if statuses == {"SUPPORTED"}
                else "UNSUPPORTED"
                if statuses == {"UNSUPPORTED"}
                else "PARTIAL"
            )
            workflow.limitations = [
                "JSON-item subset; arbitrary code and dynamic references are unsupported.",
                "Semantic facts describe static possibilities; runtime observations are separate.",
                "Endpoint and dependency descriptions are declarations, not enforcement evidence.",
            ]
    dependency_workflows = {}
    dependency_semantics = {}
    if loaded.dependency_manifest:
        for item in loaded.dependency_manifest.dependencies:
            if item.kind == "WORKFLOW":
                dependency_workflows[item.dependency_id] = normalize(
                    loaded.documents[item.path], by_path[item.path].artifact_id
                )
                if (
                    loaded.environment.extensions.get("flowdelta.semantic_profile")
                    == "core-local-v1"
                ):
                    from flowdelta.core_adapters import AdapterContext, describe_core
                    from flowdelta.dependencies import dependency_closure

                    child = dependency_workflows[item.dependency_id]
                    facts = []
                    for dependency_side in ("before", "after"):
                        if item not in dependency_closure(
                            loaded.dependency_manifest, dependency_side
                        ):
                            continue
                        facts.extend(
                            describe_core(
                                child,
                                AdapterContext(
                                    loaded.environment,
                                    child,
                                    dependency_side,
                                    loaded.dependency_manifest,
                                ),
                            )
                        )
                    dependency_semantics[item.dependency_id] = SemanticsIndex(semantics=facts)
                    for node in child.nodes:
                        node_states = {
                            fact.support for fact in facts if fact.node_id == node.node_id
                        }
                        node.semantic_support = (
                            "SUPPORTED" if node_states == {"SUPPORTED"} else "UNSUPPORTED"
                        )
                    child.semantic_support = (
                        "SUPPORTED"
                        if all(fact.support == "SUPPORTED" for fact in facts)
                        else "PARTIAL"
                    )
        for side, workflow in [("before", before), ("after", after)]:
            incomplete = any(
                fact.side == side and fact.support != "SUPPORTED"
                for index in dependency_semantics.values()
                for fact in index.semantics
            )
            if incomplete:
                if workflow.semantic_support == "SUPPORTED":
                    workflow.semantic_support = "PARTIAL"
                workflow.limitations.append(
                    "A declared child workflow has unsupported node semantics."
                )
    return Handoff(
        case=case,
        task=loaded.task,
        policy=loaded.policy,
        environment=loaded.environment,
        initial_state=loaded.initial_state,
        provenance=loaded.provenance,
        source_manifest=loaded.manifest,
        snapshots=build_snapshots(loaded),
        before=before,
        after=after,
        semantics=SemanticsIndex(semantics=semantics),
        dependency_manifest=loaded.dependency_manifest,
        dependency_workflows=dependency_workflows,
        dependency_semantics=dependency_semantics,
    )


def write_handoff(loaded: LoadedCase, handoff: Handoff, output_dir: Path) -> Path:
    """A new directory only. Input files and any existing output are never replaced."""
    target = output_dir.absolute()
    try:
        parent = target.parent.resolve(strict=True)
        target = parent / target.name
        if target.exists() or target.is_symlink():
            raise InputError("OUTPUT_ALREADY_EXISTS")
        if target.is_relative_to(loaded.root) or loaded.root.is_relative_to(target):
            raise InputError("OUTPUT_OVERLAPS_INPUT")
        if GENERATED_PATHS.intersection(loaded.raw):
            raise InputError("GENERATED_PATH_COLLISION")
        stage = Path(tempfile.mkdtemp(prefix=".flowdelta-", dir=parent))
        try:
            data = dict(loaded.raw)
            data.update(
                {
                    "before.normalized.json": json_bytes(handoff.before.model_dump(mode="json")),
                    "after.normalized.json": json_bytes(handoff.after.model_dump(mode="json")),
                    "snapshots.json": json_bytes(handoff.snapshots.model_dump(mode="json")),
                    "semantics.json": json_bytes(handoff.semantics.model_dump(mode="json")),
                    "source-manifest.json": json_bytes(
                        handoff.source_manifest.model_dump(mode="json")
                    ),
                    "dependencies.normalized.json": json_bytes(
                        DependencyWorkflowsIndex(
                            workflows=handoff.dependency_workflows,
                            semantics=handoff.dependency_semantics,
                        ).model_dump(mode="json")
                    ),
                }
            )
            refs = list(loaded.manifest.artifacts)
            ids = {ref.artifact_id for ref in refs}
            for index, (path, content) in enumerate(sorted(data.items())):
                if len(content) > MAX_FILE_BYTES:
                    raise InputError("GENERATED_FILE_SIZE_LIMIT")
                destination = stage / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path in GENERATED_PATHS:
                    artifact_id = f"generated-{index}"
                    if artifact_id in ids:
                        raise InputError("GENERATED_ID_COLLISION")
                    refs.append(
                        ArtifactRef(
                            artifact_id=artifact_id,
                            path=path,
                            sha256=digest(content),
                            size_bytes=len(content),
                            kind="generated",
                        )
                    )
            manifest = Manifest(schema_version="0.4.0", artifacts=refs)
            write_new_bytes(stage / "manifest.json", json_bytes(manifest.model_dump(mode="json")))
            # Reserve destination, then move files into that reservation without replacing it.
            # A COMPLETE marker is unnecessary: manifest is transferred last as the commit marker.
            target.mkdir(exist_ok=False)
            try:
                for entry in sorted(stage.iterdir(), key=lambda item: item.name == "manifest.json"):
                    entry.rename(target / entry.name)
            except OSError:
                # Keep an incomplete reservation for diagnosis; no manifest means not consumable.
                raise
        finally:
            shutil.rmtree(stage)
        return target
    except FileExistsError as exc:
        raise InputError("OUTPUT_ALREADY_EXISTS") from exc
    except OSError as exc:
        raise InputError("ARTIFACT_WRITE_ERROR") from exc
