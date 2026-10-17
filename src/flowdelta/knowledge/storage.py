"""Content-addressed raw files and transaction-bound PostgreSQL records."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import JsonValue
from sqlalchemy import Connection, text

from flowdelta.ingestion import InputError, digest, json_bytes


def save_raw(root: Path, payload: bytes) -> tuple[str, str]:
    checksum = digest(payload)
    relative = Path(checksum[:2]) / f"{checksum}.json"
    root = root.resolve()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve().is_relative_to(root) is False or target.is_symlink():
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
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise InputError("KNOWLEDGE_RAW_INTEGRITY_MISMATCH") from None
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix(), checksum


def save_record(
    connection: Connection,
    root: Path,
    source: str,
    external_id: str,
    document: dict[str, JsonValue],
    *,
    modified_at: str | None = None,
) -> str:
    payload = json_bytes(document)
    path, checksum = save_raw(root, payload)
    values = {
        "source": source,
        "external_id": external_id,
        "sha256": checksum,
        "path": path,
        "size": len(payload),
        "modified": modified_at,
        "document": payload.decode("utf-8"),
    }
    connection.execute(
        text("""
        INSERT INTO knowledge.source_revision(source, external_id, sha256, raw_path, size_bytes)
        VALUES (:source, :external_id, :sha256, :path, :size)
        ON CONFLICT DO NOTHING
    """),
        values,
    )
    connection.execute(
        text("""
        INSERT INTO knowledge.source_record(source, external_id, sha256, modified_at, document)
        VALUES (:source, :external_id, :sha256, CAST(:modified AS timestamptz),
                CAST(:document AS jsonb))
        ON CONFLICT (source, external_id) DO UPDATE SET
            sha256=EXCLUDED.sha256, modified_at=EXCLUDED.modified_at, document=EXCLUDED.document
        WHERE source_record.modified_at IS NULL
           OR EXCLUDED.modified_at >= source_record.modified_at
    """),
        values,
    )
    return checksum
