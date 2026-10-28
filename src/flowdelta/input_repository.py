"""Immutable profiled inputs for exact-ID handoff, with memory and local file adapters."""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from flowdelta.artifact_io import sync_directory, write_new_bytes
from flowdelta.contracts import ArtifactRef, Contract, Handoff, Identifier, NonEmpty, Sha256
from flowdelta.fixed_profile import load_profiled_handoff
from flowdelta.handoff import write_handoff
from flowdelta.ingestion import (
    InputError,
    LoadedCase,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.pipeline_io import read_handoff


class InputRecord(Contract):
    record_format: Literal["flowdelta-input-record-v1"] = "flowdelta-input-record-v1"
    record_id: Identifier
    profile_id: Identifier
    profile_revision: NonEmpty
    profile_sha256: Sha256
    case_id: Identifier
    before_snapshot_id: NonEmpty
    after_snapshot_id: NonEmpty
    handoff_sha256: Sha256
    source_manifest_sha256: Sha256


class InputRepository(Protocol):
    """Adapters accept only handoffs that round-trip through the shared file format."""

    def save(self, bundle: Path, profile: Path, *, expected_profile_sha256: str) -> InputRecord: ...

    def get(self, record: InputRecord) -> Handoff: ...

    def get_artifact(self, record: InputRecord, artifact_id: str) -> bytes: ...


def _key(profile_sha256: str, case_id: str) -> str:
    # Hashing the namespace avoids Windows case-folding and device-name collisions.
    return "input-" + digest(json_bytes([profile_sha256, case_id]))


def _payload(handoff: Handoff) -> bytes:
    return json_bytes(handoff.model_dump(mode="json"))


def _prepare(
    bundle: Path, profile: Path, expected_profile_sha256: str
) -> tuple[LoadedCase, Handoff, InputRecord]:
    loaded, handoff, validation = load_profiled_handoff(
        bundle, profile, expected_sha256=expected_profile_sha256
    )
    record = InputRecord(
        record_id=_key(validation.profile_sha256, loaded.case.case_id),
        profile_id=validation.profile_id,
        profile_revision=validation.revision,
        profile_sha256=validation.profile_sha256,
        case_id=loaded.case.case_id,
        before_snapshot_id=validation.before_snapshot_id,
        after_snapshot_id=validation.after_snapshot_id,
        handoff_sha256=digest(_payload(handoff)),
        source_manifest_sha256=digest(json_bytes(loaded.manifest.model_dump(mode="json"))),
    )
    return loaded, handoff, record


def _check_key(record: InputRecord) -> None:
    if record.record_id != _key(record.profile_sha256, record.case_id):
        raise InputError("INPUT_RECORD_ID_MISMATCH")


def _check_handoff(record: InputRecord, handoff: Handoff) -> Handoff:
    snapshots = handoff.snapshots.snapshots
    if (
        digest(_payload(handoff)) != record.handoff_sha256
        or handoff.case.case_id != record.case_id
        or [item.snapshot_id for item in snapshots]
        != [record.before_snapshot_id, record.after_snapshot_id]
        or digest(json_bytes(handoff.source_manifest.model_dump(mode="json")))
        != record.source_manifest_sha256
    ):
        raise InputError("INPUT_RECORD_INTEGRITY_MISMATCH")
    return handoff


def _artifact(handoff: Handoff, artifact_id: str) -> ArtifactRef:
    for ref in handoff.source_manifest.artifacts:
        if ref.artifact_id == artifact_id:
            return ref
    raise InputError("INPUT_ARTIFACT_NOT_FOUND")


def _write_checked_handoff(
    loaded: LoadedCase, handoff: Handoff, record: InputRecord, destination: Path
) -> None:
    # Use the consumer's actual serialization, paths, depth and size limits on both adapters.
    # The caller owns this private directory and removes it if validation fails.
    write_handoff(loaded, handoff, destination)
    _check_handoff(record, read_handoff(destination))


@dataclass(frozen=True)
class _MemoryEntry:
    record: bytes
    handoff: Handoff
    sources: dict[str, bytes]


class MemoryInputRepository:
    """Capture source bytes and return fresh objects, so consumer mutations cannot alter storage."""

    def __init__(self) -> None:
        self._entries: dict[str, _MemoryEntry] = {}

    def save(self, bundle: Path, profile: Path, *, expected_profile_sha256: str) -> InputRecord:
        loaded, handoff, record = _prepare(bundle, profile, expected_profile_sha256)
        try:
            with tempfile.TemporaryDirectory(prefix=".input-check-") as directory:
                _write_checked_handoff(loaded, handoff, record, Path(directory) / "handoff")
        except OSError as exc:
            raise InputError("INPUT_REPOSITORY_WRITE_ERROR") from exc
        entry = _MemoryEntry(
            json_bytes(record.model_dump(mode="json")),
            handoff.model_copy(deep=True),
            dict(loaded.raw),
        )
        existing = self._entries.setdefault(record.record_id, entry)
        if existing != entry:
            raise InputError("INPUT_ID_CONFLICT")
        return record

    def get(self, record: InputRecord) -> Handoff:
        _check_key(record)
        entry = self._entries.get(record.record_id)
        if entry is None:
            raise InputError("INPUT_NOT_FOUND")
        if entry.record != json_bytes(record.model_dump(mode="json")):
            raise InputError("INPUT_RECORD_INTEGRITY_MISMATCH")
        # Input depth is bounded per document, not on an artificial enclosing Handoff JSON.
        handoff = entry.handoff.model_copy(deep=True)
        return _check_handoff(record, handoff)

    def get_artifact(self, record: InputRecord, artifact_id: str) -> bytes:
        ref = _artifact(self.get(record), artifact_id)
        data = self._entries[record.record_id].sources[ref.path]
        if len(data) != ref.size_bytes or digest(data) != ref.sha256:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        return data


class FileInputRepository:
    """Local adapter only. Persist the returned record as the trusted lookup receipt.

    The root is an explicit local storage location, never an artifact URL for teammates.
    An incomplete directory has no record.json and must not be consumed as a saved input.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _existing(self, record: InputRecord) -> InputRecord:
        if self._read_record(record) != record:
            raise InputError("INPUT_ID_CONFLICT")
        self.get(record)
        return record

    def save(self, bundle: Path, profile: Path, *, expected_profile_sha256: str) -> InputRecord:
        loaded, handoff, record = _prepare(bundle, profile, expected_profile_sha256)
        try:
            root = self.root
            if root.is_relative_to(loaded.root) or loaded.root.is_relative_to(root):
                raise InputError("OUTPUT_OVERLAPS_INPUT")
            root.mkdir(parents=True, exist_ok=True)
            target = root / record.record_id
            if target.is_symlink() or target.is_junction():
                raise InputError("LINK_REFERENCE_REJECTED")
            if target.exists():
                return self._existing(record)
            stage = Path(tempfile.mkdtemp(prefix=".input-stage-", dir=root))
            try:
                _write_checked_handoff(loaded, handoff, record, stage / "handoff")
                write_new_bytes(stage / "record.json", json_bytes(record.model_dump(mode="json")))
                for directory in sorted(stage.rglob("*"), reverse=True):
                    if directory.is_dir():
                        sync_directory(directory)
                sync_directory(stage)
                try:
                    os.rename(stage, target)
                except OSError:
                    # A concurrent winner has published a complete nonempty directory.
                    # rename cannot replace it on either Windows or POSIX.
                    if target.exists():
                        return self._existing(record)
                    raise
                sync_directory(root)
            finally:
                if stage.exists():
                    # Only this invocation's staging directory is eligible for removal.
                    assert stage.resolve().parent == root
                    assert stage.name.startswith(".input-stage-")
                    shutil.rmtree(stage)
            return record
        except OSError as exc:
            raise InputError("INPUT_REPOSITORY_WRITE_ERROR") from exc

    def _root(self) -> Path:
        try:
            return self.root.resolve(strict=True)
        except OSError as exc:
            raise InputError("INPUT_NOT_FOUND") from exc

    def _read_record(self, record: InputRecord) -> InputRecord:
        _check_key(record)
        root = self._root()
        try:
            data = _read_local(root, f"{record.record_id}/record.json")
        except InputError as exc:
            if exc.code == "MISSING_ARTIFACT":
                code = (
                    "INPUT_NOT_FOUND_OR_INCOMPLETE"
                    if (root / record.record_id).exists()
                    else "INPUT_NOT_FOUND"
                )
                raise InputError(code) from exc
            raise
        return parse_file_model(InputRecord, parse_json(data))

    def get(self, record: InputRecord) -> Handoff:
        if self._read_record(record) != record:
            raise InputError("INPUT_RECORD_INTEGRITY_MISMATCH")
        root = self._root()
        # Check every component before read_handoff resolves its own root.
        _read_local(root, f"{record.record_id}/handoff/manifest.json")
        return _check_handoff(record, read_handoff(root / record.record_id / "handoff"))

    def get_artifact(self, record: InputRecord, artifact_id: str) -> bytes:
        ref = _artifact(self.get(record), artifact_id)
        data = _read_local(self._root(), f"{record.record_id}/handoff/{ref.path}")
        if len(data) != ref.size_bytes or digest(data) != ref.sha256:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        return data
