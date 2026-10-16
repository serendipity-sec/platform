"""Node references by name, such as $('Name'); found as static text, never by running code."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import NormalizedWorkflow, SourceLocation
from flowdelta.graph.changes import Path, pointer
from flowdelta.graph.control import edge_id
from flowdelta.pipeline_contracts import Change, GraphEdge, SemanticGraph

EXTENSION = "flowdelta.graph.name_references"

_NAME = r"""(?P<q>['"])(?P<name>[^'"\\]+)(?P=q)"""
_FORMS = [
    re.compile(r"\$\(\s*" + _NAME + r"\s*\)"),
    re.compile(r"\$node\[\s*" + _NAME + r"\s*\]"),
    re.compile(r"\$items\(\s*" + _NAME + r"\s*[,)]"),
]

Status = Literal[
    "NO_REFERENCES",
    "REFERENCES_UPDATED",
    "NEW_REFERENCES",
    "BROKEN_REFERENCES",
    "REFERENCES_RETARGETED",
]


@dataclass(frozen=True)
class NameReference:
    side: Literal["before", "after"]
    node_id: str
    name: str
    location: SourceLocation

    def as_json(self) -> JsonValue:
        return {
            "side": self.side,
            "node_id": self.node_id,
            "name": self.name,
            "pointer": self.location.json_pointer,
        }


def referenced_names(text: str) -> list[str]:
    """Literal names in order of first appearance; names built at runtime are not visible."""
    found = sorted(
        (match.start(), match.group("name")) for form in _FORMS for match in form.finditer(text)
    )
    names: list[str] = []
    for _, name in found:
        if name not in names:
            names.append(name)
    return names


def _strings(value: JsonValue, path: Path) -> Iterator[tuple[Path, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def find_references(
    workflow: NormalizedWorkflow, side: Literal["before", "after"]
) -> list[NameReference]:
    references: list[NameReference] = []
    for node in workflow.nodes:
        for path, text in _strings(node.parameters, ("parameters",)):
            for name in referenced_names(text):
                location = SourceLocation(
                    artifact_id=node.source.artifact_id,
                    json_pointer=pointer(node.source.json_pointer, path),
                )
                references.append(NameReference(side, node.node_id, name, location))
    return references


def add_reference_edges(
    graph: SemanticGraph, workflow: NormalizedWorkflow, references: list[NameReference]
) -> SemanticGraph:
    """DATA edge: referenced node -> referencing node. Ports carry no meaning for these edges."""
    owners: dict[str, list[str]] = {}
    for node in workflow.nodes:
        owners.setdefault(node.name, []).append(node.node_id)
    edges = list(graph.edges)
    limitations = list(graph.limitations)
    for reference in references:
        targets = owners.get(reference.name, [])
        where = f"{reference.location.artifact_id}#{reference.location.json_pointer}"
        if len(targets) != 1:
            reason = "없는 노드 이름" if not targets else "여러 노드가 쓰는 이름"
            limitations.append(
                f"{where}의 이름 참조 '{reference.name}'는 {reason}이라 연결하지 않았다."
            )
            continue
        source = targets[0]
        key = (source, reference.node_id, reference.location.json_pointer, reference.name)
        edges.append(
            GraphEdge(
                edge_id=edge_id("data", source, reference.node_id, key),
                source_node_id=source,
                target_node_id=reference.node_id,
                relation="DATA",
                output_port=0,
                input_port=0,
                target_fields=[reference.location.json_pointer],
                evidence=[reference.location],
                basis="STATIC_POSSIBILITY",
            )
        )
    return SemanticGraph(
        node_ids=graph.node_ids, edges=edges, limitations=limitations, nodes=graph.nodes
    )


def _status(
    old: str,
    after_names: set[str],
    before_old: list[NameReference],
    after_old: list[NameReference],
    after_new: list[NameReference],
) -> Status:
    if after_old:
        # The old name still being called is the risk; whether it now hits another node matters.
        return "REFERENCES_RETARGETED" if old in after_names else "BROKEN_REFERENCES"
    if before_old:
        return "REFERENCES_UPDATED"
    if after_new:
        return "NEW_REFERENCES"
    return "NO_REFERENCES"


def pointer_encloses(outer: str, inner: str) -> bool:
    """True when inner is outer or lies under it; whole tokens only, so /1 never holds /10."""
    return inner == outer or inner.startswith(outer + "/")


def _touches(change: Change, location: SourceLocation | None, reference: NameReference) -> bool:
    """Same node and artifact, and the change sits at, above or below the reference string."""
    if (
        location is None
        or change.node_id != reference.node_id
        or location.artifact_id != reference.location.artifact_id
    ):
        return False
    held = reference.location.json_pointer
    return pointer_encloses(location.json_pointer, held) or pointer_encloses(
        held, location.json_pointer
    )


def judge_renames(
    changes: list[Change],
    before: NormalizedWorkflow,
    after: NormalizedWorkflow,
    before_refs: list[NameReference],
    after_refs: list[NameReference],
) -> list[Change]:
    """Annotate node rename changes; the kind stays UNKNOWN because runtime names are invisible."""
    before_nodes = {node.node_id: node for node in before.nodes}
    after_names = {node.name for node in after.nodes}
    judged: list[Change] = []
    for change in changes:
        node = before_nodes.get(change.node_id) if change.node_id is not None else None
        old, new = change.before_value, change.after_value
        if (
            node is None
            or change.kind != "UNKNOWN"
            or change.before_source is None
            or change.before_source.json_pointer != pointer(node.source.json_pointer, ("name",))
            or not isinstance(old, str)
            or not isinstance(new, str)
        ):
            judged.append(change)
            continue
        before_old = [item for item in before_refs if item.name == old]
        after_old = [item for item in after_refs if item.name == old]
        after_new = [item for item in after_refs if item.name == new]
        # Element replacement, deletion or reordering is recorded above the reference's /value.
        related = sorted(
            item.change_id
            for item in changes
            if item.kind == "PARAMETER_CHANGED"
            and (
                any(_touches(item, item.before_source, ref) for ref in before_old)
                or any(_touches(item, item.after_source, ref) for ref in after_new)
            )
        )
        references = sorted(
            [*before_old, *after_old, *after_new],
            key=lambda item: (item.side, item.node_id, item.location.json_pointer, item.name),
        )
        verdict: JsonValue = {
            "status": _status(old, after_names, before_old, after_old, after_new),
            "references": [item.as_json() for item in references],
            "related_change_ids": list(related),
        }
        judged.append(
            change.model_copy(update={"extensions": {**change.extensions, EXTENSION: verdict}})
        )
    return judged
