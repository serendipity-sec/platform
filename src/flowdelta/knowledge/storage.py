"""Content-addressed raw files and transaction-bound PostgreSQL records."""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError, digest, json_bytes, parse_json

_TRANSACTION_STATE_KEY = "flowdelta_knowledge_storage_transaction"
_BLOB_LOCK_SQL = "SELECT pg_advisory_lock(hashtextextended(:checksum, 0))"
_BLOB_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended(:checksum, 0))"
_BLOB_REFERENCE_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM knowledge.source_revision WHERE sha256=:checksum)"
)


@dataclass
class _StorageTransactionState:
    locked_checksums: set[str] = field(default_factory=set)
    created_files: dict[Path, tuple[Path, str]] = field(default_factory=dict)


@contextmanager
def storage_transaction(engine: Engine) -> Iterator[Connection]:
    """Own the database transaction and compensate raw files if its rows roll back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        state = _StorageTransactionState()
        connection.info[_TRANSACTION_STATE_KEY] = state
        try:
            yield connection
            transaction.commit()
        except BaseException:
            if transaction.is_active:
                try:
                    transaction.rollback()
                except SQLAlchemyError:
                    connection.invalidate()
            _remove_unreferenced_files(connection, state)
            raise
        else:
            # Also remove blobs from work rolled back inside a savepoint while the
            # enclosing storage transaction later committed successfully.
            _remove_unreferenced_files(connection, state)
        finally:
            _release_blob_locks(connection, state)
            connection.info.pop(_TRANSACTION_STATE_KEY, None)
            try:
                if connection.in_transaction():
                    connection.rollback()
            except SQLAlchemyError:
                connection.invalidate()


def save_raw(root: Path, payload: bytes) -> tuple[str, str]:
    relative, checksum, _, _ = _write_raw(root, payload)
    return relative, checksum


def _write_raw(root: Path, payload: bytes) -> tuple[str, str, bool, Path]:
    checksum = digest(payload)
    relative = Path(checksum[:2]) / f"{checksum}.json"
    root = root.resolve()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise InputError("KNOWLEDGE_RAW_PATH_INVALID")
    with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    created = False
    try:
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            if target.read_bytes() != payload:
                raise InputError("KNOWLEDGE_RAW_INTEGRITY_MISMATCH") from None
        if created:
            try:
                _sync_directory(target.parent)
            except OSError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise InputError("KNOWLEDGE_RAW_SYNC_FAILED") from None
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), checksum, created, target


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_record(
    connection: Connection,
    root: Path,
    source: str,
    external_id: str,
    raw_payload: bytes,
    *,
    modified_at: str | None = None,
) -> str:
    state = connection.info.get(_TRANSACTION_STATE_KEY)
    if not isinstance(state, _StorageTransactionState) or not connection.in_transaction():
        raise InputError("KNOWLEDGE_STORAGE_TRANSACTION_REQUIRED")

    document = parse_json(raw_payload)
    payload = raw_payload
    parsed_modified_at = _parse_modified_at(modified_at)
    checksum = digest(payload)
    if checksum not in state.locked_checksums:
        connection.execute(text(_BLOB_LOCK_SQL), {"checksum": checksum}).scalar_one()
        state.locked_checksums.add(checksum)

    path, checksum, created, target = _write_raw(root, payload)
    if created:
        state.created_files[target] = (root.resolve(), checksum)
    values = {
        "source": source,
        "external_id": external_id,
        "sha256": checksum,
        "path": path,
        "size": len(payload),
        "modified": parsed_modified_at,
        "document": json_bytes(document).decode("utf-8"),
    }
    connection.execute(
        text("""
        INSERT INTO knowledge.source_revision(source, external_id, sha256, raw_path, size_bytes)
        VALUES (:source, :external_id, :sha256, :path, :size)
        ON CONFLICT DO NOTHING
    """),
        values,
    )
    result = connection.execute(
        text("""
        INSERT INTO knowledge.source_record(source, external_id, sha256, modified_at, document)
        VALUES (:source, :external_id, :sha256, CAST(:modified AS timestamptz),
                CAST(:document AS jsonb))
        ON CONFLICT (source, external_id) DO UPDATE SET
            sha256=EXCLUDED.sha256, modified_at=EXCLUDED.modified_at, document=EXCLUDED.document
        WHERE source_record.modified_at IS NULL
           OR EXCLUDED.modified_at >= source_record.modified_at
        RETURNING sha256
    """),
        values,
    ).scalar_one_or_none()
    if result is None:
        raise InputError("KNOWLEDGE_SOURCE_RECORD_STALE")
    return str(result)


def _parse_modified_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise InputError("KNOWLEDGE_MODIFIED_AT_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputError("KNOWLEDGE_MODIFIED_AT_TIMEZONE_REQUIRED")
    return parsed


def cleanup_orphaned_raw(
    engine: Engine,
    root: Path,
    *,
    older_than_seconds: int = 86_400,
) -> int:
    """Delete old content-addressed files that no committed revision references."""
    if older_than_seconds < 0:
        raise InputError("KNOWLEDGE_RAW_CLEANUP_AGE_INVALID")
    root = root.resolve()
    if not root.exists():
        return 0

    cutoff = time.time() - older_than_seconds
    removed = 0
    try:
        with engine.connect() as connection:
            for path in root.rglob("*.json"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    relative = path.relative_to(root)
                    checksum = path.stem
                    if (
                        len(relative.parts) != 2
                        or relative.parts[0] != checksum[:2]
                        or len(checksum) != 64
                        or any(char not in "0123456789abcdef" for char in checksum)
                        or path.stat().st_mtime > cutoff
                        or not path.resolve().is_relative_to(root)
                    ):
                        continue
                except (OSError, ValueError):
                    continue

                locked = False
                try:
                    connection.execute(text(_BLOB_LOCK_SQL), {"checksum": checksum}).scalar_one()
                    locked = True
                    referenced = connection.execute(
                        _BLOB_REFERENCE_SQL, {"checksum": checksum}
                    ).scalar_one()
                    connection.rollback()
                    if not referenced and not path.is_symlink():
                        path.unlink(missing_ok=True)
                        removed += 1
                finally:
                    if connection.in_transaction():
                        connection.rollback()
                    if locked:
                        connection.execute(
                            text(_BLOB_UNLOCK_SQL), {"checksum": checksum}
                        ).scalar_one()
                        if connection.in_transaction():
                            connection.rollback()
    except (OSError, SQLAlchemyError):
        raise InputError("KNOWLEDGE_RAW_CLEANUP_FAILED") from None
    return removed


def _remove_unreferenced_files(
    connection: Connection,
    state: _StorageTransactionState,
) -> None:
    for path, (root, checksum) in state.created_files.items():
        try:
            referenced = connection.execute(
                _BLOB_REFERENCE_SQL, {"checksum": checksum}
            ).scalar_one()
            if not referenced and not path.is_symlink() and path.resolve().is_relative_to(root):
                path.unlink(missing_ok=True)
        except (OSError, SQLAlchemyError, ValueError):
            # The explicit cleanup command can reclaim leftovers if the database
            # or filesystem is unavailable during transaction cleanup.
            continue
    state.created_files.clear()


def _release_blob_locks(connection: Connection, state: _StorageTransactionState) -> None:
    for checksum in state.locked_checksums:
        try:
            connection.execute(text(_BLOB_UNLOCK_SQL), {"checksum": checksum}).scalar_one()
        except SQLAlchemyError:
            connection.invalidate()
            break
    state.locked_checksums.clear()
