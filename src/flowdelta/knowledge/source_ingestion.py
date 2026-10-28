"""Source-neutral, page-bounded vulnerability ingestion with resumable cursors."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import JsonValue
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DataError, SQLAlchemyError

from flowdelta.ingestion import InputError, digest, json_bytes, parse_json
from flowdelta.knowledge.storage import (
    read_raw,
    save_raw_for_transaction,
    save_record,
    storage_transaction,
)

Cursor = dict[str, JsonValue]
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
MAX_PAGE_SIZE = 500
MAX_PAGES_PER_RUN = 1000
MAX_CURSOR_BYTES = 16 * 1024
MAX_SOURCE_RECORD_BYTES = 1024 * 1024
MAX_EXTERNAL_ID_LENGTH = 512
MAX_NORMALIZED_RECORD_BYTES = 8 * 1024 * 1024
MAX_RETRY_RECORDS_PER_RUN = 500
_SOURCE_LOCK_SQL = (
    "SELECT pg_try_advisory_lock(hashtextextended('flowdelta.source:' || :source, 0))"
)
_SOURCE_UNLOCK_SQL = (
    "SELECT pg_advisory_unlock(hashtextextended('flowdelta.source:' || :source, 0))"
)


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


@dataclass(frozen=True)
class RetryReport:
    source: str
    attempted: int
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
    """Run one source while holding a session advisory lock for the full fetch cycle."""
    source = _validated_source(adapter)
    _validate_limits(page_size, max_pages)

    try:
        with _source_lock(engine, source):
            try:
                return _ingest_source_locked(
                    engine, raw_root, adapter, source, page_size=page_size, max_pages=max_pages
                )
            except BaseException as exc:
                error_code = _ingestion_failure_code(exc)
                _mark_failed(engine, source, error_code)
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise
                if isinstance(exc, InputError):
                    raise
                raise InputError(error_code) from None
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None


def _ingest_source_locked(
    engine: Engine,
    raw_root: Path,
    adapter: SourceAdapter,
    source: str,
    *,
    page_size: int = 100,
    max_pages: int = 100,
) -> IngestionReport:
    """Fetch and persist bounded pages; each page and its cursor commit atomically."""
    interrupted = False
    try:
        with engine.begin() as connection:
            _ensure_sync_state(connection, source)
            status = connection.execute(
                text("SELECT status FROM knowledge.sync_state WHERE source=:source FOR UPDATE"),
                {"source": source},
            ).scalar_one()
            if status == "RUNNING":
                connection.execute(
                    text("""
                        UPDATE knowledge.sync_state SET status='FAILED',
                            error_code='KNOWLEDGE_SOURCE_RUN_INTERRUPTED', updated_at=now()
                        WHERE source=:source
                    """),
                    {"source": source},
                )
                interrupted = True
            else:
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
    if interrupted:
        raise InputError("KNOWLEDGE_SOURCE_RUN_INTERRUPTED")

    page_count = 0
    records_seen = 0
    records_saved = 0
    records_failed = 0

    for page_index in range(max_pages):
        try:
            page = adapter.fetch_page(deepcopy(cursor), page_size)
        except Exception:
            raise InputError("KNOWLEDGE_SOURCE_FETCH_FAILED") from None

        try:
            next_cursor = _validated_cursor(page.next_cursor)
            if not isinstance(page.complete, bool):
                raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID")
            if (
                not isinstance(page.records, tuple)
                or len(page.records) > page_size
                or any(not isinstance(record, RawSourceRecord) for record in page.records)
                or any(not isinstance(record.raw_json, bytes) for record in page.records)
            ):
                raise InputError("KNOWLEDGE_SOURCE_PAGE_INVALID")
        except InputError as exc:
            code = (
                exc.code
                if exc.code == "KNOWLEDGE_SYNC_CURSOR_INVALID"
                else "KNOWLEDGE_SOURCE_PAGE_INVALID"
            )
            raise InputError(code) from None
        except (AttributeError, TypeError):
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
            raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None
        except OSError:
            raise InputError("KNOWLEDGE_RAW_STORAGE_FAILED") from None
        except InputError:
            raise

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
    expected = json_bytes(_validated_cursor(expected_cursor)).decode("utf-8")
    next_json = _cursor_bytes(next_cursor)
    with storage_transaction(engine) as connection:
        current_matches = connection.execute(
            text("""
                SELECT cursor = CAST(:expected AS jsonb)
                FROM knowledge.sync_state WHERE source=:source FOR UPDATE
            """),
            {"source": source, "expected": expected},
        ).scalar_one()
        if not current_matches:
            raise _CursorChanged

        if (
            status != "COMPLETE"
            and connection.execute(
                text("SELECT CAST(:next AS jsonb) = CAST(:expected AS jsonb)"),
                {"next": next_json, "expected": expected},
            ).scalar_one()
        ):
            raise InputError("KNOWLEDGE_SOURCE_CURSOR_STALLED")

        for record in page.records:
            checksum = digest(record.raw_json)
            try:
                _validate_record_id(record.external_id)
            except InputError as exc:
                _record_failure(
                    connection,
                    raw_root,
                    source,
                    _failure_external_id(record.external_id, checksum),
                    record.external_id,
                    checksum,
                    exc.code,
                    record.raw_json,
                    None,
                )
                failed += 1
                continue

            normalized, modified_at, error_code = _prepare_record(record, adapter)
            if error_code is not None:
                _record_failure(
                    connection,
                    raw_root,
                    source,
                    record.external_id,
                    record.external_id,
                    checksum,
                    error_code,
                    record.raw_json,
                    modified_at,
                )
                failed += 1
                continue

            assert normalized is not None
            try:
                with connection.begin_nested():
                    save_record(
                        connection,
                        raw_root,
                        source,
                        record.external_id,
                        normalized,
                        raw_payload=record.raw_json,
                        modified_at=modified_at,
                    )
            except InputError as exc:
                if exc.code != "KNOWLEDGE_SOURCE_RECORD_STALE":
                    raise
                _record_failure(
                    connection,
                    raw_root,
                    source,
                    record.external_id,
                    record.external_id,
                    checksum,
                    exc.code,
                    record.raw_json,
                    modified_at,
                )
                failed += 1
                continue
            except DataError:
                _record_failure(
                    connection,
                    raw_root,
                    source,
                    record.external_id,
                    record.external_id,
                    checksum,
                    "KNOWLEDGE_SOURCE_RECORD_INVALID",
                    record.raw_json,
                    modified_at,
                )
                failed += 1
                continue

            _clear_record_failures(connection, source, record.external_id, checksum, modified_at)
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
        modified_at = _modified_timestamp(record.modified_at)
    except InputError as exc:
        return None, None, exc.code
    return _prepare_raw(record.raw_json, adapter, modified_at)


def _prepare_raw(
    raw_json: bytes, adapter: SourceAdapter, modified_at: str | None
) -> tuple[dict[str, JsonValue] | None, str | None, str | None]:
    if len(raw_json) > MAX_SOURCE_RECORD_BYTES:
        return None, modified_at, "KNOWLEDGE_SOURCE_RECORD_TOO_LARGE"
    try:
        source_document = parse_json(raw_json)
        normalized = adapter.normalize(source_document)
        if not _jsonb_compatible(normalized):
            return None, modified_at, "KNOWLEDGE_SOURCE_RECORD_INVALID"
        payload = json_bytes(normalized)
        if len(payload) > MAX_NORMALIZED_RECORD_BYTES:
            return None, modified_at, "KNOWLEDGE_SOURCE_RECORD_TOO_LARGE"
        normalized_document = parse_json(payload)
    except Exception:
        return None, modified_at, "KNOWLEDGE_SOURCE_RECORD_INVALID"
    return normalized_document, modified_at, None


def _validate_record_id(external_id: str) -> None:
    if (
        not isinstance(external_id, str)
        or not external_id.strip()
        or len(external_id) > MAX_EXTERNAL_ID_LENGTH
        or "\x00" in external_id
        or _contains_surrogate(external_id)
    ):
        raise InputError("KNOWLEDGE_SOURCE_RECORD_ID_INVALID")


def _failure_external_id(external_id: str, checksum: str) -> str:
    """Avoid persisting malformed or oversized provider identifiers in quarantine rows."""
    if (
        isinstance(external_id, str)
        and bool(external_id.strip())
        and len(external_id) <= MAX_EXTERNAL_ID_LENGTH
        and "\x00" not in external_id
        and not _contains_surrogate(external_id)
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
        if len(payload) > MAX_CURSOR_BYTES or not _jsonb_compatible(value):
            raise InputError("KNOWLEDGE_SYNC_CURSOR_INVALID")
        return parse_json(payload)
    except (InputError, TypeError, ValueError, RecursionError, UnicodeError):
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
    connection: Connection,
    raw_root: Path,
    source: str,
    external_id: str,
    original_external_id: object,
    checksum: str,
    error_code: str,
    raw_payload: bytes,
    modified_at: str | None,
) -> None:
    raw_path = None
    if len(raw_payload) <= MAX_SOURCE_RECORD_BYTES:
        raw_path, _ = save_raw_for_transaction(connection, raw_root, raw_payload)
    original_id_bytes = _external_id_bytes(original_external_id)
    connection.execute(
        text("""
            INSERT INTO knowledge.sync_failure(
                source, external_id, sha256, error_code, raw_path, raw_external_id, modified_at
            )
            VALUES (:source, :external_id, :sha256, :error_code, :raw_path,
                    :raw_external_id, :modified_at)
            ON CONFLICT (source, external_id, sha256) DO UPDATE SET
                error_code=EXCLUDED.error_code, raw_path=EXCLUDED.raw_path,
                raw_external_id=EXCLUDED.raw_external_id, modified_at=EXCLUDED.modified_at,
                recorded_at=now()
        """),
        {
            "source": source,
            "external_id": external_id,
            "sha256": checksum,
            "error_code": error_code,
            "raw_path": raw_path,
            "raw_external_id": original_id_bytes,
            "modified_at": modified_at,
        },
    )


def _clear_record_failures(
    connection: Connection,
    source: str,
    external_id: str,
    checksum: str,
    modified_at: str | None,
) -> None:
    connection.execute(
        text("""
            DELETE FROM knowledge.sync_failure
            WHERE source=:source AND (
                external_id=:invalid_alias OR (
                    (external_id=:external_id OR raw_external_id=:external_id_bytes)
                    AND (
                        sha256=:checksum
                        OR modified_at < CAST(:modified_at AS text)
                    )
                )
            )
        """),
        {
            "source": source,
            "external_id": external_id,
            "invalid_alias": f"invalid-{checksum}",
            "external_id_bytes": _external_id_bytes(external_id),
            "checksum": checksum,
            "modified_at": modified_at,
        },
    )


def _external_id_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or len(value) > MAX_EXTERNAL_ID_LENGTH:
        return None
    return value.encode("utf-8", "surrogatepass")


def _mark_failed(engine: Engine, source: str, error_code: str) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE knowledge.sync_state SET status='FAILED', error_code=:error_code,
                        updated_at=now()
                    WHERE source=:source
                """),
                {
                    "source": source,
                    "error_code": error_code,
                },
            )
    except SQLAlchemyError:
        # The original stable error is more useful than a driver diagnostic.
        return


@contextmanager
def _source_lock(engine: Engine, source: str) -> Iterator[None]:
    """Serialize complete runs for one source across provider calls and DB writes."""
    with engine.connect() as connection:
        try:
            acquired = connection.execute(text(_SOURCE_LOCK_SQL), {"source": source}).scalar_one()
            if not acquired:
                raise InputError("KNOWLEDGE_SOURCE_ALREADY_RUNNING")
            try:
                yield
            finally:
                try:
                    connection.execute(text(_SOURCE_UNLOCK_SQL), {"source": source})
                except SQLAlchemyError:
                    connection.invalidate()
        finally:
            if connection.in_transaction():
                connection.rollback()


def retry_quarantined_records(
    engine: Engine,
    raw_root: Path,
    adapter: SourceAdapter,
    *,
    limit: int = 100,
) -> RetryReport:
    """Retry a bounded batch of quarantined records from retained raw bytes."""
    source = _validated_source(adapter)
    if type(limit) is not int or not 1 <= limit <= MAX_RETRY_RECORDS_PER_RUN:
        raise InputError("KNOWLEDGE_INGESTION_LIMIT_INVALID")

    attempted = 0
    saved = 0
    failed = 0
    try:
        with _source_lock(engine, source):
            with storage_transaction(engine) as connection:
                rows = (
                    connection.execute(
                        text("""
                        SELECT failure.external_id, failure.sha256, failure.raw_path,
                            failure.raw_external_id, failure.modified_at,
                            (
                                SELECT count(*)
                                FROM knowledge.sync_failure AS revision
                                WHERE revision.source=failure.source
                                    AND revision.external_id=failure.external_id
                                    AND revision.modified_at IS NULL
                            ) AS untimestamped_revision_count
                        FROM knowledge.sync_failure AS failure
                        WHERE failure.source=:source AND failure.raw_path IS NOT NULL
                            AND failure.raw_external_id IS NOT NULL
                        ORDER BY failure.recorded_at, failure.external_id, failure.sha256
                        LIMIT :limit FOR UPDATE
                    """),
                        {"source": source, "limit": limit},
                    )
                    .mappings()
                    .all()
                )

                for row in rows:
                    attempted += 1
                    checksum = row["sha256"].strip()
                    try:
                        raw_payload = read_raw(raw_root, row["raw_path"], checksum)
                        external_id = row["raw_external_id"].decode("utf-8", "surrogatepass")
                    except InputError as exc:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            exc.code,
                        )
                        failed += 1
                        continue
                    except UnicodeError:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            "KNOWLEDGE_SOURCE_RECORD_ID_INVALID",
                        )
                        failed += 1
                        continue

                    try:
                        _validate_record_id(external_id)
                    except InputError as exc:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            exc.code,
                        )
                        failed += 1
                        continue

                    if row["modified_at"] is None and row["untimestamped_revision_count"] > 1:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            "KNOWLEDGE_SOURCE_REVISION_ORDER_UNKNOWN",
                        )
                        failed += 1
                        continue

                    normalized, modified_at, error_code = _prepare_raw(
                        raw_payload, adapter, row["modified_at"]
                    )
                    if error_code is not None:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            error_code,
                        )
                        failed += 1
                        continue

                    assert normalized is not None
                    try:
                        with connection.begin_nested():
                            save_record(
                                connection,
                                raw_root,
                                source,
                                external_id,
                                normalized,
                                raw_payload=raw_payload,
                                modified_at=modified_at,
                                allow_untimestamped_replacement=False,
                            )
                    except InputError as exc:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            exc.code,
                        )
                        failed += 1
                        continue
                    except DataError:
                        _update_failure_code(
                            connection,
                            source,
                            row["external_id"],
                            checksum,
                            "KNOWLEDGE_SOURCE_RECORD_INVALID",
                        )
                        failed += 1
                        continue

                    _clear_record_failures(connection, source, external_id, checksum, modified_at)
                    saved += 1
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_DATABASE_UNAVAILABLE") from None
    except OSError:
        raise InputError("KNOWLEDGE_RAW_STORAGE_FAILED") from None

    return RetryReport(source, attempted, saved, failed)


def _update_failure_code(
    connection: Connection,
    source: str,
    external_id: str,
    checksum: str,
    error_code: str,
) -> None:
    connection.execute(
        text("""
            UPDATE knowledge.sync_failure SET error_code=:error_code, recorded_at=now()
            WHERE source=:source AND external_id=:external_id AND sha256=:sha256
        """),
        {
            "source": source,
            "external_id": external_id,
            "sha256": checksum,
            "error_code": error_code,
        },
    )


def _validated_source(adapter: SourceAdapter) -> str:
    source = getattr(adapter, "source", None)
    if not isinstance(source, str) or _SOURCE_NAME.fullmatch(source) is None:
        raise InputError("KNOWLEDGE_SOURCE_NAME_INVALID")
    return source


def _validate_limits(page_size: int, max_pages: int) -> None:
    if (
        type(page_size) is not int
        or type(max_pages) is not int
        or not 1 <= page_size <= MAX_PAGE_SIZE
        or not 1 <= max_pages <= MAX_PAGES_PER_RUN
    ):
        raise InputError("KNOWLEDGE_INGESTION_LIMIT_INVALID")


def _ingestion_failure_code(error: BaseException) -> str:
    if isinstance(error, InputError):
        return error.code
    if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        return "KNOWLEDGE_SOURCE_RUN_INTERRUPTED"
    if isinstance(error, SQLAlchemyError):
        return "KNOWLEDGE_DATABASE_UNAVAILABLE"
    if isinstance(error, OSError):
        return "KNOWLEDGE_RAW_STORAGE_FAILED"
    return "KNOWLEDGE_SOURCE_RUN_FAILED"


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _jsonb_compatible(value: object) -> bool:
    pending: list[tuple[object, bool]] = [(value, False)]
    active_containers: set[int] = set()
    while pending:
        item, exiting = pending.pop()
        if isinstance(item, (dict, list)):
            identity = id(item)
            if exiting:
                active_containers.remove(identity)
                continue
            if identity in active_containers:
                return False
            active_containers.add(identity)
            pending.append((item, True))
        if isinstance(item, str):
            if "\x00" in item or _contains_surrogate(item):
                return False
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or "\x00" in key or _contains_surrogate(key):
                    return False
                pending.append((child, False))
        elif isinstance(item, list):
            pending.extend((child, False) for child in item)
        elif item is None or isinstance(item, (bool, int, float)):
            continue
        else:
            return False
    return True
