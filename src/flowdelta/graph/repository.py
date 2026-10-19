"""Immutable analysis storage keyed by contract IDs; file and in-memory implementations.

A shared-database adapter can implement the same protocol once the common migration
setup exists. Callers look analyses up by exact ID, never by "latest for this case".
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from pydantic import JsonValue

from flowdelta.ingestion import InputError, digest, json_bytes, parse_json, parse_model
from flowdelta.pipeline_contracts import AnalysisBundle

# Contract Identifier characters; "." and ".." are refused so a file path never escapes.
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True)
class AnalysisRecord:
    analysis_id: str
    case_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    producer: str
    sha256: str
    # Relative to the repository root; empty for the in-memory repository.
    location: str
    limitations: list[str] = field(default_factory=list)
    # A's analyzer metadata (e.g. analysis limits) travels in the bundle extensions.
    extensions: dict[str, JsonValue] = field(default_factory=dict)


class AnalysisRepository(Protocol):
    def save(self, bundle: AnalysisBundle) -> AnalysisRecord: ...

    def get(self, analysis_id: str) -> AnalysisBundle: ...

    def find(
        self, case_id: str, before_snapshot_id: str, after_snapshot_id: str
    ) -> list[AnalysisRecord]: ...


def _checked_id(analysis_id: str) -> str:
    if not _ID.match(analysis_id) or analysis_id in {".", ".."}:
        raise InputError("ANALYSIS_ID_INVALID")
    return analysis_id


def _unlinked(path: Path, analysis_id: str) -> Path:
    # A link under the root could send a write or a read outside it, so none is followed.
    if path.is_symlink() or path.is_junction():
        raise InputError("ANALYSIS_LINK_REJECTED", location=analysis_id)
    return path


def _encode(bundle: AnalysisBundle) -> bytes:
    payload = json_bytes(bundle.model_dump(mode="json"))
    # Refuse what get() could not read back (e.g. JSON_DEPTH_LIMIT) instead of storing it.
    _decode(payload, bundle.analysis_id)
    return payload


def _decode(payload: bytes, analysis_id: str) -> AnalysisBundle:
    bundle = parse_model(AnalysisBundle, parse_json(payload))
    if bundle.analysis_id != analysis_id:
        raise InputError("ANALYSIS_INTEGRITY_MISMATCH", location=analysis_id)
    return bundle


def _record(bundle: AnalysisBundle, checksum: str, location: str) -> AnalysisRecord:
    return AnalysisRecord(
        analysis_id=bundle.analysis_id,
        case_id=bundle.case_id,
        before_snapshot_id=bundle.before_snapshot_id,
        after_snapshot_id=bundle.after_snapshot_id,
        producer=bundle.authorship.producer,
        sha256=checksum,
        location=location,
        limitations=list(bundle.limitations),
        extensions=dict(bundle.extensions),
    )


def _matches(record: AnalysisRecord, case_id: str, before: str, after: str) -> bool:
    return (record.case_id, record.before_snapshot_id, record.after_snapshot_id) == (
        case_id,
        before,
        after,
    )


class MemoryAnalysisRepository:
    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}

    def save(self, bundle: AnalysisBundle) -> AnalysisRecord:
        analysis_id = _checked_id(bundle.analysis_id)
        payload = _encode(bundle)
        stored = self._payloads.setdefault(analysis_id, payload)
        if stored != payload:
            raise InputError("ANALYSIS_ID_CONFLICT", location=analysis_id)
        return _record(bundle, digest(payload), "")

    def get(self, analysis_id: str) -> AnalysisBundle:
        payload = self._payloads.get(_checked_id(analysis_id))
        if payload is None:
            raise InputError("ANALYSIS_NOT_FOUND", location=analysis_id)
        return _decode(payload, analysis_id)

    def find(
        self, case_id: str, before_snapshot_id: str, after_snapshot_id: str
    ) -> list[AnalysisRecord]:
        records = [
            _record(self.get(analysis_id), digest(payload), "")
            for analysis_id, payload in sorted(self._payloads.items())
        ]
        return [
            item
            for item in records
            if _matches(item, case_id, before_snapshot_id, after_snapshot_id)
        ]


class FileAnalysisRepository:
    """One folder per analysis ID holding `<sha256>.json`; the file name is its integrity check."""

    def __init__(self, root: Path) -> None:
        # Fixed now, so a later change of working directory still finds the same records.
        self.root = root.absolute()

    def _files(self, analysis_id: str) -> list[Path]:
        folder = _unlinked(self.root / _checked_id(analysis_id), analysis_id)
        if not folder.is_dir():
            return []
        return sorted(_unlinked(item, analysis_id) for item in folder.glob("*.json"))

    def save(self, bundle: AnalysisBundle) -> AnalysisRecord:
        payload = _encode(bundle)
        checksum = digest(payload)
        target = self.root / _checked_id(bundle.analysis_id) / f"{checksum}.json"
        existing = self._files(bundle.analysis_id)
        if existing and existing != [target]:
            raise InputError("ANALYSIS_ID_CONFLICT", location=bundle.analysis_id)
        if not existing:
            target.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != payload:
                    raise InputError(
                        "ANALYSIS_INTEGRITY_MISMATCH", location=bundle.analysis_id
                    ) from None
            finally:
                temporary.unlink(missing_ok=True)
        return _record(bundle, checksum, target.relative_to(self.root).as_posix())

    def _load(self, analysis_id: str) -> tuple[AnalysisBundle, Path]:
        files = self._files(analysis_id)
        if not files:
            raise InputError("ANALYSIS_NOT_FOUND", location=analysis_id)
        if len(files) > 1:
            raise InputError("ANALYSIS_ID_CONFLICT", location=analysis_id)
        payload = files[0].read_bytes()
        if digest(payload) != files[0].stem:
            raise InputError("ANALYSIS_INTEGRITY_MISMATCH", location=analysis_id)
        return _decode(payload, analysis_id), files[0]

    def get(self, analysis_id: str) -> AnalysisBundle:
        return self._load(analysis_id)[0]

    def find(
        self, case_id: str, before_snapshot_id: str, after_snapshot_id: str
    ) -> list[AnalysisRecord]:
        if not self.root.is_dir():
            return []
        records = []
        for folder in sorted(self.root.iterdir()):
            if not _unlinked(folder, folder.name).is_dir():
                continue
            bundle, path = self._load(folder.name)
            records.append(_record(bundle, path.stem, path.relative_to(self.root).as_posix()))
        return [
            item
            for item in records
            if _matches(item, case_id, before_snapshot_id, after_snapshot_id)
        ]
