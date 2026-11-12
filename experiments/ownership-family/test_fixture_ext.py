"""Run: uv run --frozen --extra baseline pytest experiments/ownership-family -q"""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flowdelta.baseline.fixture import OrderStore  # noqa: E402

from fixture_ext import app, internal_read  # noqa: E402

INITIAL = Path(__file__).resolve().parents[2] / "examples/order-lookup/state/initial.json"


def store(tmp_path: Path) -> OrderStore:
    return OrderStore(tmp_path, INITIAL, "run-ext")


def test_internal_route_without_owner_returns_any_order_and_records_it(tmp_path: Path) -> None:
    orders = store(tmp_path)
    body = internal_read(orders, "ORD-2001", None)
    assert body["customer_id"] == "other-demo"
    (event,) = orders.state()["events"]
    assert (event["resource_id"], event["outcome"], event["principal"]) == (
        "ORD-2001",
        "RETURNED",
        "customer-demo",
    )


def test_owner_filter_hides_other_customers_and_records_the_refusal(tmp_path: Path) -> None:
    orders = store(tmp_path)
    assert internal_read(orders, "ORD-1001", "customer-demo")["order_id"] == "ORD-1001"
    with pytest.raises(HTTPException) as denied:
        internal_read(orders, "ORD-2001", "customer-demo")
    assert denied.value.status_code == 404
    outcomes = [(e["resource_id"], e["outcome"]) for e in orders.state()["events"]]
    assert outcomes == [("ORD-1001", "RETURNED"), ("ORD-2001", "NOT_FOUND")]


def test_state_is_unchanged_by_reads(tmp_path: Path) -> None:
    orders = store(tmp_path)
    before = orders.state()["orders_sha256"]
    internal_read(orders, "ORD-2001", None)
    assert orders.state()["orders_sha256"] == before


def test_no_api_schema_or_docs() -> None:
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)
