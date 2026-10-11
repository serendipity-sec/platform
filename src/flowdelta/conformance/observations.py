"""Read a stopped, local fixture database and preserve node ports and item-link metadata."""

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from flowdelta.contracts import SourceLocation
from flowdelta.ingestion import MAX_FILE_BYTES, InputError, digest, json_bytes, parse_json
from flowdelta.pipeline_contracts import ItemLink, NodeObservation, ObservedItem


def decode_reference_table(data: str) -> dict[str, Any]:
    """Decode n8n's JSON reference table, rejecting cycles rather than inventing JSON values."""
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise InputError("EXECUTION_DATA_SIZE_LIMIT")
    table = parse_json(b'{"table":' + encoded + b"}")["table"]
    if not isinstance(table, list) or not table or len(table) > 100000:
        raise InputError("EXECUTION_REFERENCE_TABLE_INVALID")
    memo: dict[int, Any] = {}
    active: set[int] = set()

    def entry(index: int, depth: int) -> Any:
        if depth > 64 or index < 0 or index >= len(table) or index in active:
            raise InputError("EXECUTION_REFERENCE_TABLE_INVALID")
        if index in memo:
            return memo[index]
        active.add(index)
        value = table[index]

        def member(item: Any) -> Any:
            if isinstance(item, str):
                if not item.isascii() or not item.isdecimal():
                    raise InputError("EXECUTION_REFERENCE_TABLE_INVALID")
                return entry(int(item), depth + 1)
            if isinstance(item, dict | list):
                raise InputError("EXECUTION_REFERENCE_TABLE_INVALID")
            return item

        result: Any
        if isinstance(value, dict):
            result = {key: member(item) for key, item in value.items()}
        elif isinstance(value, list):
            result = [member(item) for item in value]
        else:
            result = value
        active.remove(index)
        memo[index] = result
        return result

    result = entry(0, 0)
    if not isinstance(result, dict):
        raise InputError("EXECUTION_DATA_OBJECT_REQUIRED")
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower()
            in {
                "authorization",
                "cookie",
                "x-flowdelta-client",
                "x-flowdelta-service",
                "resumetoken",
                "set-cookie",
            }
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def collect_database(database: Path, output: Path) -> dict[str, Any]:
    if not database.is_file() or output.exists():
        raise InputError("EXECUTION_DATABASE_OR_OUTPUT_INVALID")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT e.id, e.status, e.workflowId, d.workflowData, d.data "
            "FROM execution_entity e JOIN execution_data d ON e.id=d.executionId ORDER BY e.id"
        ).fetchall()
    executions = []
    observations = []
    for identity, status, workflow_id, workflow_data, encoded in rows:
        workflow = json.loads(workflow_data)
        data = decode_reference_table(encoded)
        # No encrypted credentials, config, resume token, or entire DB is published.
        record = _redact(
            {
                "execution_id": str(identity),
                "status": status,
                "workflow_id": workflow_id,
                "workflow": {
                    key: workflow.get(key)
                    for key in ["id", "name", "nodes", "connections", "settings"]
                },
                "data": {
                    "resultData": data.get("resultData", {}),
                    "startData": data.get("startData", {}),
                },
            }
        )
        executions.append(record)
        observations.extend(observe_execution(record, len(executions) - 1))
    result = {
        "schema_version": "0.4.0",
        "authorship": "RUNTIME_OBSERVATION",
        "source_database_sha256": digest(database.read_bytes()),
        "transformation": "Selected fields; authentication headers redacted; no resume tokens.",
        "executions": executions,
        "observations": [item.model_dump(mode="json") for item in observations],
    }
    output.write_bytes(json_bytes(result))
    return result


def observe_execution(record: dict[str, Any], execution_index: int) -> list[NodeObservation]:
    ids = {node["name"]: node["id"] for node in record["workflow"]["nodes"]}
    result = []
    for name, attempts in record["data"]["resultData"].get("runData", {}).items():
        if name not in ids:
            raise InputError("OBSERVED_NODE_NOT_IN_WORKFLOW")
        for run_index, attempt in enumerate(attempts):
            for output_type, ports in attempt.get("data", {}).items():
                for output_index, items in enumerate(ports):
                    parsed = []
                    for index, item in enumerate(items or []):
                        pairs = item.get("pairedItem")
                        links = []
                        link_status: Literal["ABSENT", "OBSERVED", "UNSUPPORTED"] = (
                            "ABSENT" if pairs is None else "OBSERVED"
                        )
                        for pair in (
                            pairs if isinstance(pairs, list) else ([] if pairs is None else [pairs])
                        ):
                            if type(pair) is int:
                                links.append(ItemLink(input_index=0, item_index=pair))
                            elif isinstance(pair, dict) and not set(pair) - {"item", "input"}:
                                links.append(
                                    ItemLink(
                                        input_index=pair.get("input", 0), item_index=pair["item"]
                                    )
                                )
                            else:
                                link_status = "UNSUPPORTED"
                        parsed.append(
                            ObservedItem(
                                item_index=index,
                                json_data=item.get("json", {}),
                                paired_items=links,
                                linkage_status=link_status,
                                raw_metadata={k: v for k, v in item.items() if k != "json"},
                            )
                        )
                    result.append(
                        NodeObservation(
                            execution_id=record["execution_id"],
                            workflow_id=record["workflow_id"],
                            node_id=ids[name],
                            node_name=name,
                            node_run_index=run_index,
                            output_type=output_type,
                            output_index=output_index,
                            items=parsed,
                            execution_status="SUCCESS"
                            if attempt.get("executionStatus") == "success"
                            else "ERROR"
                            if attempt.get("executionStatus") == "error"
                            else "UNKNOWN",
                            source=SourceLocation(
                                artifact_id="runtime-executions",
                                json_pointer=(
                                    f"/executions/{execution_index}/data/resultData/runData/"
                                    f"{name.replace('~', '~0').replace('/', '~1')}/"
                                    f"{run_index}/data/{output_type}/{output_index}"
                                ),
                            ),
                        )
                    )
    return result
