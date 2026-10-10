"""Produce an immutable local handoff from the exact bytes validated by the loader."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from flowdelta.contracts import (
    ArtifactRef,
    Handoff,
    Manifest,
    SemanticsIndex,
    Snapshot,
    SnapshotIndex,
)
from flowdelta.ingestion import MAX_FILE_BYTES, InputError, LoadedCase, digest, json_bytes
from flowdelta.normalize import describe_nodes, normalize, structural_digest
from flowdelta.semantics import PROFILE, describe_profile

GENERATED_PATHS = {
    "before.normalized.json",
    "after.normalized.json",
    "semantics.json",
    "snapshots.json",
    "source-manifest.json",
    "manifest.json",
}


def build_handoff(loaded: LoadedCase) -> Handoff:
    case = loaded.case
    by_path = {ref.path: ref for ref in loaded.manifest.artifacts}
    before = normalize(loaded.documents[case.before_ref], by_path[case.before_ref].artifact_id)
    after = normalize(loaded.documents[case.after_ref], by_path[case.after_ref].artifact_id)
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
                "No dependency resolution or semantic equivalence hash is implemented.",
            ]
    contexts = [by_path[path] for path in ["case.json", *case.references()[2:]]]
    snapshots = []
    for side, path in [("before", case.before_ref), ("after", case.after_ref)]:
        ref = by_path[path]
        identity = digest(
            json_bytes(
                {
                    "schema_version": "0.3.0",
                    "normalizer_version": "structure-v1",
                    "case_id": case.case_id,
                    "side": side,
                    "workflow": ref.model_dump(mode="json"),
                    "context": [item.model_dump(mode="json") for item in contexts],
                }
            )
        )
        # Explicit branch keeps the two-sided contract typed without inferring runtime semantics.
        typed_side: Literal["before", "after"] = "before" if side == "before" else "after"
        snapshots.append(
            Snapshot(
                snapshot_id=f"snapshot-{identity}",
                case_id=case.case_id,
                side=typed_side,
                workflow_ref=ref,
                context_refs=contexts,
                raw_sha256=ref.sha256,
                structural_sha256=structural_digest(loaded.documents[path]),
            )
        )
    return Handoff(
        case=case,
        task=loaded.task,
        policy=loaded.policy,
        environment=loaded.environment,
        initial_state=loaded.initial_state,
        provenance=loaded.provenance,
        source_manifest=loaded.manifest,
        snapshots=SnapshotIndex(snapshots=snapshots),
        before=before,
        after=after,
        semantics=SemanticsIndex(semantics=semantics),
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
            manifest = Manifest(schema_version="0.3.0", artifacts=refs)
            (stage / "manifest.json").write_bytes(json_bytes(manifest.model_dump(mode="json")))
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
