"""Observe publication from inside the pinned n8n container, using its SQLite ABI."""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from flowdelta.ingestion import InputError, json_bytes

PUBLICATION_QUERY = (
    "SELECT e.activeVersionId AS active, p.publishedVersionId AS published, h.versionId AS history "
    "FROM workflow_entity e "
    "LEFT JOIN workflow_published_version p ON p.workflowId=e.id "
    "LEFT JOIN workflow_history h "
    "ON h.workflowId=e.id AND h.versionId=p.publishedVersionId WHERE e.id=?"
)
PROBE_SCRIPT = """
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('/home/node/.n8n/database.sqlite', { readOnly: true });
try {
    const query = db.prepare(process.argv[1]);
    const ids = JSON.parse(process.argv[2]);
    process.stdout.write(JSON.stringify(ids.map(id => query.get(id) || {})));
} finally {
    db.close();
}
"""


def _query_rows(container: str, workflow_ids: list[str]) -> list[dict[str, Any]]:
    # Host-side SQLite can conflict with a live Linux WAL on Windows bind mounts.
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "node",
            "-e",
            PROBE_SCRIPT,
            PUBLICATION_QUERY,
            json.dumps(workflow_ids),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("INVALID_PUBLICATION_PROBE")
    return rows


def publication_snapshot(container: str, workflow_ids: list[str]) -> dict[str, Any]:
    states = []
    try:
        rows = _query_rows(container, workflow_ids)
        for workflow_id, row in zip(workflow_ids, rows, strict=True):
            active, published, history = (
                row.get(key) for key in ("active", "published", "history")
            )
            states.append(
                {
                    "workflow_id": workflow_id,
                    "active_version_id": active,
                    "published_version_id": published,
                    "history_present": history is not None,
                    "ready": bool(active) and active == published == history,
                }
            )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"ready": False, "workflows": states, "error_code": type(exc).__name__}
    return {"ready": bool(states) and all(item["ready"] for item in states), "workflows": states}


def wait_for_publication(
    container: str, workflow_ids: list[str], evidence: Path, *, timeout: float = 60
) -> None:
    # HTTP readiness precedes asynchronous publication reconciliation in n8n 2.42.6.
    # Wait for the version mapping actually read by getPublishedWorkflowData().
    deadline = time.monotonic() + timeout
    while True:
        snapshot = publication_snapshot(container, workflow_ids)
        evidence.write_bytes(json_bytes(snapshot))
        if snapshot["ready"]:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InputError("CONFORMANCE_PUBLICATION_TIMEOUT")
        time.sleep(min(0.5, remaining))
