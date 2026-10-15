import json
import sqlite3
from pathlib import Path

import pytest

from flowdelta.conformance.publication import publication_snapshot, wait_for_publication
from flowdelta.ingestion import InputError


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "database.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE workflow_entity (id TEXT, activeVersionId TEXT);"
            "CREATE TABLE workflow_published_version (workflowId TEXT, publishedVersionId TEXT);"
            "CREATE TABLE workflow_history (workflowId TEXT, versionId TEXT);"
            "INSERT INTO workflow_entity VALUES ('parent', 'v-parent'), ('child', 'v-child');"
            "INSERT INTO workflow_history VALUES ('parent', 'v-parent'), ('child', 'v-child');"
            "INSERT INTO workflow_published_version VALUES ('parent', 'v-parent');"
        )
    return path


@pytest.mark.parametrize("child_version", [None, "stale"])
def test_active_child_is_not_ready_without_current_publication(
    database: Path, child_version: str | None
) -> None:
    if child_version:
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO workflow_published_version VALUES ('child', ?)", (child_version,)
            )
    snapshot = publication_snapshot(database, ["parent", "child"])
    assert not snapshot["ready"]
    assert snapshot["workflows"][0]["ready"]
    assert not snapshot["workflows"][1]["ready"]


def test_publication_requires_history_for_the_same_workflow(database: Path) -> None:
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO workflow_published_version VALUES ('child', 'v-child')")
        db.execute("UPDATE workflow_history SET workflowId='unrelated' WHERE versionId='v-child'")
    assert not publication_snapshot(database, ["parent", "child"])["ready"]


def test_wait_observes_reconciled_child_before_returning(database: Path, monkeypatch) -> None:
    def reconcile(_: float) -> None:
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO workflow_published_version VALUES ('child', 'v-child')")

    monkeypatch.setattr("flowdelta.conformance.publication.time.sleep", reconcile)
    evidence = database.parent / "publication.json"
    wait_for_publication(database, ["parent", "child"], evidence)
    snapshot = json.loads(evidence.read_bytes())
    assert snapshot["ready"]
    assert all(item["ready"] for item in snapshot["workflows"])


def test_timeout_preserves_unready_evidence(database: Path) -> None:
    evidence = database.parent / "publication.json"
    with pytest.raises(InputError, match="CONFORMANCE_PUBLICATION_TIMEOUT"):
        wait_for_publication(database, ["parent", "child"], evidence, timeout=0)
    assert not json.loads(evidence.read_bytes())["ready"]


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite"
    assert not publication_snapshot(database, ["child"])["ready"]
    assert not database.exists()
