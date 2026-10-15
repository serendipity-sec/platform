"""Before/after differences with original JSON Pointers; classification never implies risk."""

from collections.abc import Iterator
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import Connection, NormalizedNode, NormalizedWorkflow, SourceLocation
from flowdelta.graph.control import short_hash
from flowdelta.pipeline_contracts import Change

ChangeKind = Literal[
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
Token = str | int
Path = tuple[Token, ...]

# Raw n8n node keys outside id/name/type/typeVersion/parameters land in opaque_fields.
DISPLAY_KEYS = {"position", "notes", "notesInFlow", "color"}
SETTINGS_KEYS = {
    "disabled",
    "onError",
    "retryOnFail",
    "maxTries",
    "waitBetweenTries",
    "alwaysOutputData",
    "executeOnce",
    "continueOnFail",
}
# Workflow-level metadata without execution meaning; other unknown keys stay UNKNOWN.
WORKFLOW_DISPLAY_KEYS = {"name", "tags", "meta", "versionId"}


def pointer(base: str, path: Path) -> str:
    escaped = (str(token).replace("~", "~0").replace("/", "~1") for token in path)
    return base + "".join(f"/{token}" for token in escaped)


def _same(before: JsonValue, after: JsonValue) -> bool:
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(_same(before[k], after[k]) for k in before)
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _same(b, a) for b, a in zip(before, after, strict=True)
        )
    return type(before) is type(after) and before == after


def _item_ids(items: list[JsonValue]) -> list[str] | None:
    """n8n lists such as assignments carry unique `id`s; match by them when all items do."""
    ids = [
        item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if len(ids) != len(items) or len(set(ids)) != len(ids):
        return None
    return [str(item) for item in ids]


Leaf = tuple[Path | None, Path | None, JsonValue, JsonValue]


def diff_values(
    before: JsonValue, after: JsonValue, before_path: Path = (), after_path: Path = ()
) -> Iterator[Leaf]:
    """Yield (before path, after path, before value, after value); a missing side is None."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if key not in after:
                yield (*before_path, key), None, before[key], None
            elif key not in before:
                yield None, (*after_path, key), None, after[key]
            else:
                yield from diff_values(
                    before[key], after[key], (*before_path, key), (*after_path, key)
                )
        return
    if isinstance(before, list) and isinstance(after, list):
        before_ids, after_ids = _item_ids(before), _item_ids(after)
        if before_ids is not None and after_ids is not None:
            # IDs avoid false changes after insertion, but array order may affect execution.
            # Preserve a reorder of surviving items instead of treating every ID list as a set.
            common = set(before_ids) & set(after_ids)
            if [item for item in before_ids if item in common] != [
                item for item in after_ids if item in common
            ]:
                yield before_path, after_path, before, after
                return
            after_index = {item_id: index for index, item_id in enumerate(after_ids)}
            for index, item_id in enumerate(before_ids):
                if item_id in after_index:
                    other = after_index[item_id]
                    yield from diff_values(
                        before[index], after[other], (*before_path, index), (*after_path, other)
                    )
                else:
                    yield (*before_path, index), None, before[index], None
            known = set(before_ids)
            for index, item_id in enumerate(after_ids):
                if item_id not in known:
                    yield None, (*after_path, index), None, after[index]
            return
        for index in range(max(len(before), len(after))):
            if index >= len(after):
                yield (*before_path, index), None, before[index], None
            elif index >= len(before):
                yield None, (*after_path, index), None, after[index]
            else:
                yield from diff_values(
                    before[index], after[index], (*before_path, index), (*after_path, index)
                )
        return
    if not _same(before, after):
        yield before_path, after_path, before, after


def _location(source: SourceLocation, path: Path | None) -> SourceLocation | None:
    if path is None:
        return None
    return SourceLocation(
        artifact_id=source.artifact_id, json_pointer=pointer(source.json_pointer, path)
    )


def _workflow_root(workflow: NormalizedWorkflow) -> SourceLocation:
    return SourceLocation(artifact_id=workflow.source_artifact_id, json_pointer="")


def _change(
    kind: ChangeKind,
    node_id: str | None,
    before_value: JsonValue,
    after_value: JsonValue,
    before_source: SourceLocation | None,
    after_source: SourceLocation | None,
) -> Change:
    change_id = "change-" + short_hash(
        kind,
        node_id,
        before_source.json_pointer if before_source else None,
        after_source.json_pointer if after_source else None,
    )
    return Change(
        change_id=change_id,
        node_id=node_id,
        kind=kind,
        before_value=before_value,
        after_value=after_value,
        before_source=before_source,
        after_source=after_source,
    )


def _node_summary(node: NormalizedNode) -> JsonValue:
    return {
        "name": node.name,
        "node_type": node.node_type,
        "type_version": node.type_version,
        "parameters": node.parameters,
    }


def _opaque_kind(key: str) -> ChangeKind:
    if key in DISPLAY_KEYS:
        return "DISPLAY_CHANGED"
    if key in SETTINGS_KEYS:
        return "SETTINGS_CHANGED"
    return "UNKNOWN"


def node_changes(before: NormalizedNode, after: NormalizedNode) -> list[Change]:
    node_id = before.node_id
    changes: list[Change] = []

    def add(
        kind: ChangeKind, path_b: Path | None, path_a: Path | None, b: JsonValue, a: JsonValue
    ) -> None:
        changes.append(
            _change(
                kind,
                node_id,
                b,
                a,
                _location(before.source, path_b),
                _location(after.source, path_a),
            )
        )

    # A different node type under the same ID is effectively another node; not interpreted.
    if before.node_type != after.node_type:
        add("UNKNOWN", ("type",), ("type",), before.node_type, after.node_type)
    if not _same(before.type_version, after.type_version):
        add(
            "TYPE_VERSION_CHANGED",
            ("typeVersion",),
            ("typeVersion",),
            before.type_version,
            after.type_version,
        )
    # Expressions may reference nodes by name, so a rename is not assumed to be display-only.
    if before.name != after.name:
        add("UNKNOWN", ("name",), ("name",), before.name, after.name)
    for path_b, path_a, b, a in diff_values(
        before.parameters, after.parameters, ("parameters",), ("parameters",)
    ):
        add("PARAMETER_CHANGED", path_b, path_a, b, a)
    for key in sorted(set(before.opaque_fields) | set(after.opaque_fields)):
        b_value = before.opaque_fields.get(key)
        a_value = after.opaque_fields.get(key)
        in_b, in_a = key in before.opaque_fields, key in after.opaque_fields
        if in_b and in_a and _same(b_value, a_value):
            continue
        if (
            key == "credentials"
            and isinstance(b_value or {}, dict)
            and isinstance(a_value or {}, dict)
        ):
            b_slots = b_value if isinstance(b_value, dict) else {}
            a_slots = a_value if isinstance(a_value, dict) else {}
            for slot in sorted(set(b_slots) | set(a_slots)):
                if slot in b_slots and slot in a_slots and _same(b_slots[slot], a_slots[slot]):
                    continue
                add(
                    "CREDENTIAL_CHANGED",
                    (key, slot) if slot in b_slots else None,
                    (key, slot) if slot in a_slots else None,
                    b_slots.get(slot),
                    a_slots.get(slot),
                )
            continue
        add(
            _opaque_kind(key),
            (key,) if in_b else None,
            (key,) if in_a else None,
            b_value,
            a_value,
        )
    return changes


def _connection_key(connection: Connection) -> tuple[str, str, int, str, str, int]:
    return (
        connection.source_node_id,
        connection.source_output_type,
        connection.source_output_index,
        connection.target_node_id,
        connection.target_input_type,
        connection.target_input_index,
    )


def _connection_value(connection: Connection) -> JsonValue:
    return {
        "source_node_id": connection.source_node_id,
        "output_type": connection.source_output_type,
        "output_port": connection.source_output_index,
        "target_node_id": connection.target_node_id,
        "input_type": connection.target_input_type,
        "input_port": connection.target_input_index,
    }


def connection_changes(before: NormalizedWorkflow, after: NormalizedWorkflow) -> list[Change]:
    before_keys = {_connection_key(item): item for item in before.connections}
    after_keys = {_connection_key(item): item for item in after.connections}
    changes = [
        _change("CONNECTION_CHANGED", key[0], _connection_value(item), None, item.source, None)
        for key, item in before_keys.items()
        if key not in after_keys
    ]
    changes += [
        _change("CONNECTION_CHANGED", key[0], None, _connection_value(item), None, item.source)
        for key, item in after_keys.items()
        if key not in before_keys
    ]
    return changes


def workflow_changes(before: NormalizedWorkflow, after: NormalizedWorkflow) -> list[Change]:
    changes: list[Change] = []
    b_root, a_root = _workflow_root(before), _workflow_root(after)
    for key in sorted(set(before.opaque_fields) | set(after.opaque_fields)):
        if key == "settings":
            for path_b, path_a, b, a in diff_values(
                before.opaque_fields.get(key, {}),
                after.opaque_fields.get(key, {}),
                (key,),
                (key,),
            ):
                changes.append(
                    _change(
                        "SETTINGS_CHANGED",
                        None,
                        b,
                        a,
                        _location(b_root, path_b),
                        _location(a_root, path_a),
                    )
                )
            continue
        in_b, in_a = key in before.opaque_fields, key in after.opaque_fields
        b_value, a_value = before.opaque_fields.get(key), after.opaque_fields.get(key)
        if in_b and in_a and _same(b_value, a_value):
            continue
        kind: ChangeKind = "DISPLAY_CHANGED" if key in WORKFLOW_DISPLAY_KEYS else "UNKNOWN"
        changes.append(
            _change(
                kind,
                None,
                b_value,
                a_value,
                _location(b_root, (key,) if in_b else None),
                _location(a_root, (key,) if in_a else None),
            )
        )
    return changes


def extract_changes(before: NormalizedWorkflow, after: NormalizedWorkflow) -> list[Change]:
    after_nodes = {node.node_id: node for node in after.nodes}
    before_ids = {node.node_id for node in before.nodes}
    changes: list[Change] = []
    for node in before.nodes:
        counterpart = after_nodes.get(node.node_id)
        if counterpart is None:
            changes.append(
                _change("REMOVED", node.node_id, _node_summary(node), None, node.source, None)
            )
        else:
            changes.extend(node_changes(node, counterpart))
    for node in after.nodes:
        if node.node_id not in before_ids:
            changes.append(
                _change("ADDED", node.node_id, None, _node_summary(node), None, node.source)
            )
    changes.extend(connection_changes(before, after))
    changes.extend(workflow_changes(before, after))
    return sorted(
        changes,
        key=lambda item: (
            item.node_id or "",
            item.kind,
            item.before_source.json_pointer if item.before_source else "",
            item.after_source.json_pointer if item.after_source else "",
        ),
    )
