"""Local synthetic order service, with independent state and append-only business events."""

import hmac
import json
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as PathParameter

from flowdelta.ingestion import digest, json_bytes

NORMAL_PROFILE = "order-lookup-local-v1"
OWNERSHIP_PROFILE = "order-ownership-eval-v1"


class OrderStore:
    def __init__(
        self,
        directory: Path,
        initial: Path,
        run_id: str,
        *,
        profile: str = NORMAL_PROFILE,
        request_id: str = "",
    ) -> None:
        if profile not in {NORMAL_PROFILE, OWNERSHIP_PROFILE}:
            raise RuntimeError("UNKNOWN_FIXTURE_PROFILE")
        if profile == OWNERSHIP_PROFILE and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
            raise RuntimeError("EVALUATION_REQUEST_ID_REQUIRED")
        if profile == NORMAL_PROFILE and request_id:
            raise RuntimeError("REQUEST_ID_REQUIRES_EVALUATION_PROFILE")
        self.profile = profile
        self.request_id = request_id
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "orders.sqlite3"
        self.run_id = run_id
        initial_bytes = initial.read_bytes()
        source = json.loads(initial_bytes)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, data TEXT)")
            current = db.execute("SELECT value FROM metadata WHERE key='run_id'").fetchone()
            if current and current[0] != run_id:
                raise RuntimeError("STATE_BELONGS_TO_ANOTHER_RUN")
            if current:
                original_hash = db.execute(
                    "SELECT value FROM metadata WHERE key='initial_sha256'"
                ).fetchone()
                if not original_hash or original_hash[0] != digest(initial_bytes):
                    raise RuntimeError("INITIAL_STATE_CHANGED")
                metadata = dict(db.execute("SELECT key, value FROM metadata"))
                # Old normal databases predate these fields; they cannot become evaluation runs.
                if (
                    metadata.get("fixture_profile", NORMAL_PROFILE) != profile
                    or metadata.get("request_id", "") != request_id
                ):
                    raise RuntimeError("FIXTURE_CONTEXT_CHANGED")
            if not current:
                for order in source["data"]["orders"]:
                    db.execute(
                        "INSERT INTO orders VALUES (?, ?)", (order["order_id"], json.dumps(order))
                    )
                db.execute("INSERT INTO metadata VALUES ('run_id', ?)", (run_id,))
                db.execute(
                    "INSERT INTO metadata VALUES ('initial_sha256', ?)", (digest(initial_bytes),)
                )
                db.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [("fixture_profile", profile), ("request_id", request_id)],
                )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def lookup(self, order_id: str) -> dict[str, str]:
        return self._read(order_id, access="scoped")

    def require_evaluation(self) -> None:
        if self.profile != OWNERSHIP_PROFILE:
            raise HTTPException(status_code=404, detail="not_found")

    def request_context(self) -> dict[str, str]:
        self.require_evaluation()
        # Set by the fixture launcher, never by a workflow request body or header.
        return {
            "principal": "customer-demo",
            "tenant": "tenant-demo",
            "request_id": self.request_id,
            "run_id": self.run_id,
        }

    def ownership(self, order_id: str) -> dict[str, str]:
        self.require_evaluation()
        return self._read(order_id, access="ownership-metadata")

    def internal_lookup(self, order_id: str) -> dict[str, str]:
        self.require_evaluation()
        return self._read(order_id, access="service")

    def _read(
        self, order_id: str, *, access: Literal["scoped", "ownership-metadata", "service"]
    ) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
            order = json.loads(row[0]) if row else None
            permitted = order is not None and (
                access != "scoped"
                or (order["customer_id"] == "customer-demo" and order["tenant_id"] == "tenant-demo")
            )
            body = {"detail": "not_found"}
            if permitted and order is not None:
                fields = (
                    ("order_id", "customer_id", "tenant_id")
                    if access == "ownership-metadata"
                    else ("order_id", "status")
                )
                body = {field: str(order[field]) for field in fields}
            event: dict[str, Any] = {
                "schema_version": "0.4.0",
                "run_id": self.run_id,
                "producer": "orders-api",
                "observed_at": datetime.now(UTC).isoformat(),
                "event_type": "ORDER_METADATA_READ"
                if access == "ownership-metadata"
                else "ORDER_READ",
                "principal": "customer-demo",
                "tenant": "tenant-demo",
                "resource_id": order_id,
                "method": "GET",
                "outcome": "RETURNED" if permitted else "NOT_FOUND",
                "response_sha256": digest(json_bytes(body)),
            }
            if self.profile == OWNERSHIP_PROFILE:
                event["attributes"] = {
                    "fixture_profile": self.profile,
                    "request_id": self.request_id,
                    "access_path": access,
                    "resource_customer_id": order["customer_id"] if order else None,
                    "resource_tenant_id": order["tenant_id"] if order else None,
                }
            db.execute("INSERT INTO events(data) VALUES (?)", (json.dumps(event),))
        if not permitted:
            raise HTTPException(status_code=404, detail="not_found")
        return body

    def state(self) -> dict[str, Any]:
        with self.connect() as db:
            orders = [
                json.loads(row[0]) for row in db.execute("SELECT data FROM orders ORDER BY id")
            ]
            metadata = dict(db.execute("SELECT key, value FROM metadata"))
            events = [
                {**json.loads(row[1]), "sequence": row[0]}
                for row in db.execute("SELECT seq,data FROM events ORDER BY seq")
            ]
        return {
            "schema_version": "0.4.0",
            "run_id": self.run_id,
            "metadata": metadata,
            "orders": orders,
            "orders_sha256": digest(json_bytes(orders)),
            "events": events,
        }


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.store = OrderStore(
        Path(os.environ["FD_STATE_DIR"]),
        Path(os.environ["FD_INITIAL_STATE"]),
        os.environ["FD_RUN_ID"],
        profile=os.environ.get("FD_FIXTURE_PROFILE", NORMAL_PROFILE),
        request_id=os.environ.get("FD_REQUEST_ID", ""),
    )
    application.state.service_token = os.environ["FD_SERVICE_TOKEN"]
    application.state.control_token = os.environ["FD_CONTROL_TOKEN"]
    if not application.state.service_token or not application.state.control_token:
        raise RuntimeError("FIXTURE_CREDENTIAL_REQUIRED")
    yield


# No interactive docs and no OpenAPI schema: the schema listed every route without
# authentication (w12 security check). Clients use the documented routes directly.
app = FastAPI(
    title="FlowDelta local order fixture",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def authenticate(actual: str | None, expected: str) -> None:
    if actual is None or not hmac.compare_digest(actual, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/orders/lookup")
def lookup(
    order_id: Annotated[str, Query(pattern=r"^ORD-[0-9]{4}$")],
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    authenticate(x_flowdelta_service, app.state.service_token)
    result: dict[str, str] = app.state.store.lookup(order_id)
    return result


@app.get("/__fixture__/state")
def state(x_flowdelta_control: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    authenticate(x_flowdelta_control, app.state.control_token)
    result: dict[str, Any] = app.state.store.state()
    return result


def evaluation_access(service_token: str | None) -> OrderStore:
    store: OrderStore = app.state.store
    store.require_evaluation()
    authenticate(service_token, app.state.service_token)
    return store


@app.get("/internal/request-context")
def request_context(
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    return evaluation_access(x_flowdelta_service).request_context()


@app.get("/internal/orders/{order_id}/ownership")
def ownership(
    order_id: Annotated[str, PathParameter(pattern=r"^ORD-[0-9]{4}$")],
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    return evaluation_access(x_flowdelta_service).ownership(order_id)


@app.get("/internal/orders/{order_id}")
def internal_lookup(
    order_id: Annotated[str, PathParameter(pattern=r"^ORD-[0-9]{4}$")],
    request: Request,
    x_flowdelta_service: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    store = evaluation_access(x_flowdelta_service)
    # An experiment's ?owner= filter must not silently become an unrestricted read here.
    if request.query_params:
        raise HTTPException(status_code=422, detail="query_filters_not_supported")
    return store.internal_lookup(order_id)
