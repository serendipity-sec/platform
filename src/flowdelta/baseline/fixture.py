"""Local synthetic order service, with independent state and append-only business events."""

import hmac
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query

from flowdelta.ingestion import digest, json_bytes


class OrderStore:
    def __init__(self, directory: Path, initial: Path, run_id: str) -> None:
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
            if not current:
                for order in source["data"]["orders"]:
                    db.execute(
                        "INSERT INTO orders VALUES (?, ?)", (order["order_id"], json.dumps(order))
                    )
                db.execute("INSERT INTO metadata VALUES ('run_id', ?)", (run_id,))
                db.execute(
                    "INSERT INTO metadata VALUES ('initial_sha256', ?)", (digest(initial_bytes),)
                )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def lookup(self, order_id: str) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
            order = json.loads(row[0]) if row else None
            permitted = (
                order
                and order["customer_id"] == "customer-demo"
                and order["tenant_id"] == "tenant-demo"
            )
            body = (
                {"order_id": order["order_id"], "status": order["status"]}
                if permitted and order
                else {"error": "not_found"}
            )
            event = {
                "schema_version": "0.4.0",
                "run_id": self.run_id,
                "producer": "orders-api",
                "observed_at": datetime.now(UTC).isoformat(),
                "event_type": "ORDER_READ",
                "principal": "customer-demo",
                "tenant": "tenant-demo",
                "resource_id": order_id,
                "method": "GET",
                "outcome": "RETURNED" if permitted else "NOT_FOUND",
                "response_sha256": digest(json_bytes(body)),
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
    )
    application.state.service_token = os.environ["FD_SERVICE_TOKEN"]
    application.state.control_token = os.environ["FD_CONTROL_TOKEN"]
    if not application.state.service_token or not application.state.control_token:
        raise RuntimeError("FIXTURE_CREDENTIAL_REQUIRED")
    yield


app = FastAPI(
    title="FlowDelta local order fixture", lifespan=lifespan, docs_url=None, redoc_url=None
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
