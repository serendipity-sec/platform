"""Order fixture with one extra internal route, for the ownership regression family (#28).

The registered fixture (`flowdelta.baseline.fixture`) only has `/orders/lookup`, which enforces
ownership itself. Real services often also have an internal route that trusts its caller.
This app adds that route so a workflow change can move the ownership check out of the service.

- `GET /internal/orders/{order_id}`: service token only. Returns any order, unless `owner` is
  given, in which case orders of another customer are reported as not found.
- Every read, allowed or not, is recorded as an ORDER_READ event with its outcome.
- The principal is fixed: every webhook call in a run comes from the one local client,
  `customer-demo` (the same assumption as the registered fixture).

This file is experiment code. It does not change the registered runtime or its image.
"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Path, Query
from flowdelta.baseline.fixture import OrderStore, authenticate, lifespan
from flowdelta.ingestion import digest, json_bytes

PRINCIPAL = "customer-demo"
ORDER_ID = r"^ORD-[0-9]{4}$"

app = FastAPI(
    title="FlowDelta ownership family fixture",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def internal_read(store: OrderStore, order_id: str, owner: str | None) -> dict[str, str]:
    with store.connect() as db:
        row = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        order = json.loads(row[0]) if row else None
        found = order is not None and (owner is None or order["customer_id"] == owner)
        body = (
            {
                "order_id": order["order_id"],
                "status": order["status"],
                "customer_id": order["customer_id"],
                "tenant_id": order["tenant_id"],
            }
            if found and order
            else {"error": "not_found"}
        )
        event = {
            "schema_version": "0.4.0",
            "run_id": store.run_id,
            "producer": "orders-api",
            "observed_at": datetime.now(UTC).isoformat(),
            "event_type": "ORDER_READ",
            "route": "internal",
            "principal": PRINCIPAL,
            "tenant": "tenant-demo",
            "resource_id": order_id,
            "owner_filter": owner,
            "method": "GET",
            "outcome": "RETURNED" if found else "NOT_FOUND",
            "response_sha256": digest(json_bytes(body)),
        }
        db.execute("INSERT INTO events(data) VALUES (?)", (json.dumps(event),))
    if not found:
        raise HTTPException(status_code=404, detail="not_found")
    return body


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/orders/lookup")
def lookup(
    order_id: Annotated[str, Query(pattern=ORDER_ID)],
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    authenticate(x_flowdelta_service, app.state.service_token)
    result: dict[str, str] = app.state.store.lookup(order_id)
    return result


@app.get("/internal/orders/{order_id}")
def internal(
    order_id: Annotated[str, Path(pattern=ORDER_ID)],
    owner: Annotated[str | None, Query(max_length=64)] = None,
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    authenticate(x_flowdelta_service, app.state.service_token)
    return internal_read(app.state.store, order_id, owner)


@app.get("/__fixture__/state")
def state(x_flowdelta_control: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    authenticate(x_flowdelta_control, app.state.control_token)
    result: dict[str, Any] = app.state.store.state()
    return result
