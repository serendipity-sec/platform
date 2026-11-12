from pathlib import Path

import pytest
from conftest import EXAMPLE
from fastapi import HTTPException

from flowdelta.baseline.fixture import OrderStore, app, authenticate
from flowdelta.pipeline_contracts import EvidenceEvent


def test_normal_read_records_independent_event_without_changing_order(tmp_path: Path) -> None:
    store = OrderStore(tmp_path / "first", EXAMPLE / "state/initial.json", "run-first")
    initial = store.state()
    assert store.lookup("ORD-1001") == {"order_id": "ORD-1001", "status": "준비중"}
    final = store.state()
    assert final["orders_sha256"] == initial["orders_sha256"]
    assert len(final["events"]) == 1
    event = EvidenceEvent.model_validate(final["events"][0])
    assert event.run_id == "run-first" and event.resource_id == "ORD-1001"
    fresh = OrderStore(tmp_path / "second", EXAMPLE / "state/initial.json", "run-second")
    assert fresh.state()["events"] == []
    assert fresh.state()["orders_sha256"] == initial["orders_sha256"]
    with pytest.raises(RuntimeError, match="STATE_BELONGS_TO_ANOTHER_RUN"):
        OrderStore(tmp_path / "first", EXAMPLE / "state/initial.json", "run-second")


def test_fixture_fixed_principal_and_credentials(tmp_path: Path) -> None:
    store = OrderStore(tmp_path, EXAMPLE / "state/initial.json", "run-local")
    with pytest.raises(HTTPException) as denied:
        store.lookup("ORD-2001")
    assert denied.value.status_code == 404
    for actual in [None, "different"]:
        with pytest.raises(HTTPException) as invalid:
            authenticate(actual, "fixture-local-test-value")
        assert invalid.value.status_code == 401
    authenticate("fixture-local-test-value", "fixture-local-test-value")


def test_fixture_publishes_no_api_schema_or_docs() -> None:
    # The unauthenticated OpenAPI schema listed every route (w12 security check).
    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
