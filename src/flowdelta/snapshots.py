"""Content-address declared configuration, without claiming behavioral equivalence."""

from copy import deepcopy
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import ResolvedDependency, Snapshot, SnapshotIndex
from flowdelta.dependencies import dependency_closure, workflow_dependency_limits
from flowdelta.ingestion import LoadedCase, digest, json_bytes
from flowdelta.normalize import structural_digest


def configuration_node(node: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The shared node configuration projection; only display coordinates are omitted."""
    return {key: value for key, value in node.items() if key != "position"}


def configuration_workflow(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Only node display coordinates are omitted. All unknown fields remain significant."""
    result = deepcopy(document)
    nodes = result.get("nodes")
    if isinstance(nodes, list):
        result["nodes"] = [
            configuration_node(node) if isinstance(node, dict) else node for node in nodes
        ]
    return result


def build_snapshots(loaded: LoadedCase) -> SnapshotIndex:
    case = loaded.case
    by_path = {ref.path: ref for ref in loaded.manifest.artifacts}
    contexts = [by_path[path] for path in ["case.json", *case.references()[2:]]]
    snapshots = []
    for side in ("before", "after"):
        typed_side: Literal["before", "after"] = side
        path = case.before_ref if side == "before" else case.after_ref
        ref = by_path[path]
        declared = (
            dependency_closure(loaded.dependency_manifest, typed_side)
            if loaded.dependency_manifest is not None
            else []
        )
        dependencies = [
            ResolvedDependency(
                dependency_id=item.dependency_id,
                kind=item.kind,
                version=item.version,
                artifact_ref=by_path[item.path],
                workflow_id=item.workflow_id,
                requires=item.requires,
            )
            for item in declared
        ]
        supplied_ids = {item.workflow_id for item in declared if item.workflow_id is not None}
        limits = workflow_dependency_limits(loaded.documents[path], supplied_ids)
        for item in declared:
            if item.kind == "WORKFLOW":
                direct_ids = {
                    child.workflow_id
                    for child in declared
                    if child.dependency_id in item.requires and child.workflow_id is not None
                }
                limits.extend(workflow_dependency_limits(loaded.documents[item.path], direct_ids))
        if loaded.dependency_manifest is None:
            limits.append("DEPENDENCY_MANIFEST_NOT_SUPPLIED")
        limits = sorted(set(limits))
        configuration = {
            "hash_version": "config-v1",
            "workflow": configuration_workflow(loaded.documents[path]),
            "task": loaded.documents[case.task_ref],
            "policy": loaded.documents[case.evaluation_policy_ref],
            "environment": loaded.documents[case.environment_ref],
            "initial_state": loaded.documents[case.initial_state_ref],
            "dependencies": [
                {
                    "id": item.dependency_id,
                    "kind": item.kind,
                    "version": item.version,
                    "workflow_id": item.workflow_id,
                    "requires": sorted(item.requires),
                    "document": configuration_workflow(loaded.documents[item.path])
                    if item.kind == "WORKFLOW"
                    else loaded.documents[item.path],
                }
                for item in declared
            ],
        }
        semantic_hash = digest(json_bytes(configuration)) if not limits else None
        identity = digest(
            json_bytes(
                {
                    "case_id": case.case_id,
                    "side": side,
                    "normalizer_version": "structure-v1",
                    "workflow": ref.model_dump(mode="json"),
                    "contexts": [item.model_dump(mode="json") for item in contexts],
                    "dependencies": [item.model_dump(mode="json") for item in dependencies],
                    "semantic_hash": semantic_hash,
                }
            )
        )
        snapshots.append(
            Snapshot(
                snapshot_id=f"snapshot-{identity}",
                case_id=case.case_id,
                side=typed_side,
                workflow_ref=ref,
                context_refs=contexts,
                raw_sha256=ref.sha256,
                structural_sha256=structural_digest(loaded.documents[path]),
                semantic_sha256=semantic_hash,
                semantic_hash_version="config-v1" if semantic_hash else None,
                semantic_hash_scope="DECLARED_CONFIGURATION" if semantic_hash else None,
                dependency_status="DECLARED_RESOLVED" if not limits else "UNRESOLVED",
                dependencies=dependencies,
                dependency_limits=limits,
            )
        )
    return SnapshotIndex(snapshots=snapshots)
