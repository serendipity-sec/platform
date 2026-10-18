"""Source-neutral, page-bounded vulnerability ingestion with resumable cursors."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import JsonValue
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError, digest, json_bytes, parse_json
from flowdelta.knowledge.storage import save_record, storage_transaction

Cursor = dict[str, JsonValue]
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
MAX_PAGE_SIZE = 500
MAX_PAGES_PER_RUN = 1000
MAX_CURSOR_BYTES = 16 * 1024
MAX_SOURCE_RECORD_BYTES = 1024 * 1024
MAX_EXTERNAL_ID_LENGTH = 512
MAX_NORMALIZED_RECORD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RawSourceRecord:
    """One source record before normalization; raw_json is kept byte-for-byte."""

    external_id: str
    raw_json: bytes
    modified_at: datetime | None = None


@dataclass(frozen=True)
class SourcePage:
    """A bounded source page and the opaque cursor to use for the next request."""

    records: tuple[RawSourceRecord, ...]
    next_cursor: Cursor
    complete: bool


class SourceAdapter(Protocol):
    """A source connector supplies pages and maps each JSON record to the common form."""

    source: str

    def fetch_page(self, cursor: Cursor, page_size: int) -> SourcePage: ...

    def normalize(self, document: dict[str, JsonValue]) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class IngestionReport:
    source: str
    status: Literal["COMPLETE", "PENDING"]
    pages: int
    records_seen: int
    records_saved: int
    records_failed: int


def ingest_source(
    engine: Engine,
    raw_root: Path,
    adapter: SourceAdapter,
    *,
    page_size: int = 100,
    max_pages: int = 100,
) -> IngestionReport:
    """Fetch and persist bounded pages; each page and its cursor commit atomically."""
    source = getattr(adapter, "source", None)
    if not isinstance(source, str) or _SOURCE_NAME.fullmatch(source) is None:
        raise InputError("KNOWLEDGE_SOURCE_NAME_INVALID")
    if (
        type(page_size) is not int
        or type(max_pages) is not int
        or not 1 <= page_size <= MAX_PAGE_SIZE
        or not 1 <= max_pages <= MAX_PAGES_PER_RUN
    ):
        raise InputError("KNOWLEDGE_INGESTION_LIMIT_INVALID")

    try:
        with engine.begin() as connection:
            _ensure_sync_state(connection, source)
            cursor = _load_cursor(connection, source, lock=True)
            connection.execute(
                text("""
                    UPDATE knowledge.sync_state SET status='RUNNING', error_code=NULL,
                        updated_at=now()
                    WHERE source=:source
                """),
                {"source": source},
            )
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None

    page_count = 0
    records_seen = 0
    records_saved = 0
    records_failed = 0

    for page_index in range(max_pages):
        try:
            page = adapter.fetch_page(deepcopy(cursor), page_size)
        except Exception:
            _mark_failed(engine, source, cursor, "KNOWLEDGE_SOURCE_FETCH_FAILED")
            raise InputError("KNOWLEDGE_SOURCE_FETCH_FAILED") from None

        try:
            next_cursor = _validated_cursor(page.next_cursor)
            if not isinstance(page.complete, bool):
                raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID")
            if (
                not isinstance(page.records, tuple)
                or len(page.records) > page_size
                or any(not isinstance(record, RawSourceRecord) for record in page.records)
            ):
                raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID")
            if not page.complete and _cursor_bytes(next_cursor) == _cursor_bytes(cursor):
                raise InputError("KNOWLEDGE_SOURCE_CURSOR_STALLED")
        except InputError as exc:
            code = (
                exc.code
                if exc.code in {"KNOWLEDGE_SOURCE_CURSOR_STALLED", "KNOWLEDGE_SYNC_CURSOR_INVALID"}
                else "KNOWLEDGE_SOURCE_PAGE_INVALID"
            )
            _mark_failed(engine, source, cursor, code)
            raise InputError(code) from None
        except (AttributeError, TypeError):
            _mark_failed(engine, source, cursor, "KNOWLEDGE_SOURCE_PAGE_INVALID")
            raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID") from None

        final_page = page.complete
        next_status: Literal["RUNNING", "PENDING", "COMPLETE"] = (
            "COMPLETE" if final_page else "PENDING" if page_index == max_pages - 1 else "RUNNING"
        )
        try:
            saved, failed = _commit_page(
                engine,
                raw_root,
                source,
                cursor,
                next_cursor,
                page,
                adapter,
                next_status,
            )
        except _CursorChanged:
            raise InputError("KNOWLEDGE_SYNC_CURSOR_CHANGED") from None
        except SQLAlchemyError:
            _mark_failed(engine, source, cursor, "KNOWLEDGE_DATABASE_UNAVAILABLE")
            raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None
        except OSError:
            _mark_failed(engine, source, cursor, "KNOWLEDGE_RAW_STORAGE_FAILED")
            raise InputError("KNOWLEDGE_RAW_STORAGE_FAILED") from None
        except InputError:
            _mark_failed(engine, source, cursor, "KNOWLEDGE_SOURCE_PAGE_INVALID")
            raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID") from None

        page_count += 1
        records_seen += len(page.records)
        records_saved += saved
        records_failed += failed
        cursor = next_cursor
        if final_page:
            return IngestionReport(
                source, "COMPLETE", page_count, records_seen, records_saved, records_failed
            )

    return IngestionReport(
        source, "PENDING", page_count, records_seen, records_saved, records_failed
    )


class _CursorChanged(Exception):
    """Another runner committed a newer source cursor before this page."""


def _commit_page(
    engine: Engine,
    raw_root: Path,
    source: str,
    expected_cursor: Cursor,
    next_cursor: Cursor,
    page: SourcePage,
    adapter: SourceAdapter,
    status: Literal["RUNNING", "PENDING", "COMPLETE"],
) -> tuple[int, int]:
    saved = 0
    failed = 0
    expected = _cursor_bytes(expected_cursor)
    next_json = _cursor_bytes(next_cursor)
    with storage_transaction(engine) as connection:
        current = _load_cursor(connection, source, lock=True)
        if _cursor_bytes(current) != expected:
            raise _CursorChanged

        for record in page.records:
            if not isinstance(record.raw_json, bytes):
                raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID")
            checksum = digest(record.raw_json)
            try:
                _validate_record_id(record.external_id)
            except InputError as exc:
                _record_failure(
                    connection,
                    source,
                    _failure_external_id(record.external_id, checksum),
                    checksum,
                    exc.code,
                )
                failed += 1
                continue

            normalized, modified_at, error_code = _prepare_record(record, adapter)
            if error_code is not None:
                _record_failure(connection, source, record.external_id, checksum, error_code)
                failed += 1
                continue

            assert normalized is not None
            save_record(
                connection,
                raw_root,
                source,
                record.external_id,
                normalized,
                raw_payload=record.raw_json,
                modified_at=modified_at,
            )
            connection.execute(
                text("""
                    DELETE FROM knowledge.sync_failure
                    WHERE source=:source AND external_id=:external_id AND sha256=:sha256
                """),
                {"source": source, "external_id": record.external_id, "sha256": checksum},
            )
            saved += 1

        connection.execute(
            text("""
                UPDATE knowledge.sync_state SET cursor=CAST(:cursor AS jsonb),
                    status=:status, records_seen=records_seen + :count,
                    updated_at=now(), error_code=NULL
                WHERE source=:source
            """),
            {
                "source": source,
                "cursor": next_json,
                "status": status,
                "count": len(page.records),
            },
        )
    return saved, failed


def _prepare_record(
    record: RawSourceRecord, adapter: SourceAdapter
) -> tuple[dict[str, JsonValue] | None, str | None, str | None]:
    if len(record.raw_json) > MAX_SOURCE_RECORD_BYTES:
        return None, None, "KNOWLEDGE_SOURCE_RECORD_TOO_LARGE"
    try:
        source_document = parse_json(record.raw_json)
        normalized = adapter.normalize(source_document)
        payload = json_bytes(normalized)
        if len(payload) > MAX_NORMALIZED_RECORD_BYTES:
            return None, None, "KNOWLEDGE_SOURCE_RECORD_TOO_LARGE"
        normalized_document = parse_json(payload)
        modified_at = _modified_timestamp(record.modified_at)
    except Exception:
        return None, None, "KNOWLEDGE_SOURCE_RECORD_INVALID"
    return normalized_document, modified_at, None


def _validate_record_id(external_id: str) -> None:
    if (
        not isinstance(external_id, str)
        or not external_id.strip()
        or len(external_id) > MAX_EXTERNAL_ID_LENGTH
    ):
        raise InputError("KNOWLEDGE_SOURCE_RECORD_ID_INVALID")


def _failure_external_id(external_id: str, checksum: str) -> str:
    """Avoid persisting malformed or oversized provider identifiers in quarantine rows."""
    if (
        isinstance(external_id, str)
        and bool(external_id.strip())
        and len(external_id) <= MAX_EXTERNAL_ID_LENGTH
    ):
        return external_id
    return f"invalid-{checksum}"


def _modified_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InputError("KNOWLEDGE_SOURCE_MODIFIED_AT_INVALID")
    return value.astimezone(UTC).isoformat()


def _validated_cursor(value: object) -> Cursor:
    if not isinstance(value, dict):
        raise InputError("KNOWLEDGE_SYNC_CURSOR_INVALID")
    try:
        payload = json_bytes(value)
        if len(payload) > MAX_CURSOR_BYTES:
            raise InputError("KNOWLEDGE_SYNC_CURSOR_INVALID")
        return parse_json(payload)
    except (TypeError, ValueError, RecursionError):
        raise InputError("KNOWLEDGE_SYNC_CURSOR_INVALID") from None


def _cursor_bytes(value: object) -> str:
    return json_bytes(_validated_cursor(value)).decode("utf-8")


def _ensure_sync_state(connection: Connection, source: str) -> None:
    connection.execute(
        text("""
            INSERT INTO knowledge.sync_state(source, cursor, status)
            VALUES (:source, CAST('{}' AS jsonb), 'PENDING')
            ON CONFLICT (source) DO NOTHING
        """),
        {"source": source},
    )


def _load_cursor(connection: Connection, source: str, *, lock: bool) -> Cursor:
    suffix = " FOR UPDATE" if lock else ""
    value = connection.execute(
        text(f"SELECT cursor FROM knowledge.sync_state WHERE source=:source{suffix}"),
        {"source": source},
    ).scalar_one()
    return _validated_cursor(value)


def _record_failure(
    connection: Connection, source: str, external_id: str, checksum: str, error_code: str
) -> None:
    connection.execute(
        text("""
            INSERT INTO knowledge.sync_failure(source, external_id, sha256, error_code)
            VALUES (:source, :external_id, :sha256, :error_code)
            ON CONFLICT (source, external_id, sha256) DO UPDATE SET
                error_code=EXCLUDED.error_code, recorded_at=now()
        """),
        {
            "source": source,
            "external_id": external_id,
            "sha256": checksum,
            "error_code": error_code,
        },
    )


def _mark_failed(engine: Engine, source: str, cursor: Cursor, error_code: str) -> None:
    try:
        with engine.begin() as connection:
            _ensure_sync_state(connection, source)
            connection.execute(
                text("""
                    UPDATE knowledge.sync_state SET status='FAILED', error_code=:error_code,
                        updated_at=now()
                    WHERE source=:source AND cursor=CAST(:cursor AS jsonb)
                """),
                {
                    "source": source,
                    "cursor": _cursor_bytes(cursor),
                    "error_code": error_code,
                },
            )
    except SQLAlchemyError:
        # The original stable error is more useful than a driver diagnostic.
        return
