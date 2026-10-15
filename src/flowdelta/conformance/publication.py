"""Observe publication of registered workflows in the pinned n8n fixture database."""

import sqlite3
import time
from pathlib import Path
from typing import Any

from flowdelta.ingestion import InputError, json_bytes


def publication_snapshot(database: Path, workflow_ids: list[str]) -> dict[str, Any]:
    states = []
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            for workflow_id in workflow_ids:
                row = db.execute(
                    "SELECT e.activeVersionId, p.publishedVersionId, h.versionId "
                    "FROM workflow_entity e "
                    "LEFT JOIN workflow_published_version p ON p.workflowId=e.id "
                    "LEFT JOIN workflow_history h "
                    "ON h.workflowId=e.id AND h.versionId=p.publishedVersionId WHERE e.id=?",
                    (workflow_id,),
                ).fetchone()
                active, published, history = row if row else (None, None, None)
                states.append(
                    {
                        "workflow_id": workflow_id,
                        "active_version_id": active,
                        "published_version_id": published,
                        "history_present": history is not None,
                        "ready": bool(active) and active == published == history,
                    }
                )
    except sqlite3.Error as exc:
        return {"ready": False, "workflows": states, "error_code": type(exc).__name__}
    return {"ready": bool(states) and all(item["ready"] for item in states), "workflows": states}


def wait_for_publication(
    database: Path, workflow_ids: list[str], evidence: Path, *, timeout: float = 60
) -> None:
    # HTTP readiness precedes asynchronous publication reconciliation in n8n 2.42.6.
    # Wait for the version mapping actually read by getPublishedWorkflowData().
    deadline = time.monotonic() + timeout
    while True:
        snapshot = publication_snapshot(database, workflow_ids)
        evidence.write_bytes(json_bytes(snapshot))
        if snapshot["ready"]:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InputError("CONFORMANCE_PUBLICATION_TIMEOUT")
        time.sleep(min(0.5, remaining))
