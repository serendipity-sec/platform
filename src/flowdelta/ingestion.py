"""Read only bounded local JSON bytes. No imports, expressions or network execution."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from pydantic import JsonValue, ValidationError

from flowdelta.contracts import (
    ComparisonCase,
    Contract,
    Environment,
    InitialState,
    Manifest,
    Policy,
    Provenance,
    TaskContract,
    validate_relative_path,
)

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 64


class InputError(Exception):
    """Stable diagnostic codes, never input values or filesystem exception messages."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise InputError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(_: str) -> Never:
    raise InputError("NON_FINITE_JSON_NUMBER")


def parse_json(data: bytes) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
        )
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise InputError("JSON_DEPTH_LIMIT")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise InputError("NON_FINITE_JSON_NUMBER")
            elif isinstance(item, str):
                item.encode("utf-8")  # Reject unpaired surrogates before serializing artifacts.
        if not isinstance(value, dict):
            raise InputError("JSON_OBJECT_REQUIRED")
        return value
    except RecursionError as exc:
        raise InputError("JSON_DEPTH_LIMIT") from exc
    except (UnicodeError, ValueError) as exc:
        raise InputError("INVALID_JSON") from exc


def parse_model[T: Contract](model: type[T], value: dict[str, JsonValue]) -> T:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        # Pydantic error strings can contain raw credentials from the input.
        raise InputError("CONTRACT_INVALID") from exc


def _read_local(root: Path, relative: str) -> bytes:
    try:
        validate_relative_path(relative)
        candidate = root
        for component in relative.split("/"):
            candidate = candidate / component
            if candidate.is_symlink() or candidate.is_junction():
                raise InputError("LINK_REFERENCE_REJECTED")
        if not candidate.resolve().is_relative_to(root):
            raise InputError("REFERENCE_OUTSIDE_BUNDLE")
        if not candidate.is_file():
            raise InputError("MISSING_ARTIFACT")
        # Bounded read also checks files which grew after stat().
        with candidate.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InputError("FILE_SIZE_LIMIT")
        return data
    except ValueError as exc:
        raise InputError("INVALID_REFERENCE") from exc
    except OSError as exc:
        raise InputError("ARTIFACT_READ_ERROR") from exc


@dataclass(frozen=True)
class LoadedCase:
    root: Path
    case: ComparisonCase
    manifest: Manifest
    task: TaskContract
    policy: Policy
    environment: Environment
    initial_state: InitialState
    provenance: Provenance
    raw: dict[str, bytes]
    documents: dict[str, dict[str, JsonValue]]


def load_case(bundle_dir: Path) -> LoadedCase:
    try:
        root = bundle_dir.resolve(strict=True)
        if not root.is_dir():
            raise InputError("BUNDLE_DIRECTORY_REQUIRED")
    except OSError as exc:
        raise InputError("BUNDLE_DIRECTORY_REQUIRED") from exc

    manifest = parse_model(Manifest, parse_json(_read_local(root, "manifest.json")))
    raw: dict[str, bytes] = {}
    documents: dict[str, dict[str, JsonValue]] = {}
    total = 0
    for artifact in manifest.artifacts:
        if artifact.kind != "source":
            raise InputError("SOURCE_ARTIFACT_REQUIRED")
        data = _read_local(root, artifact.path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")
        if len(data) != artifact.size_bytes or digest(data) != artifact.sha256:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        raw[artifact.path] = data
        documents[artifact.path] = parse_json(data)

    if "case.json" not in documents:
        raise InputError("CASE_NOT_IN_MANIFEST")
    case = parse_model(ComparisonCase, documents["case.json"])
    expected = {"case.json", *case.references()}
    if expected != documents.keys():
        # Dependencies need an explicit later contract, never an implicit file scan.
        raise InputError("MANIFEST_REFERENCE_MISMATCH")
    task = parse_model(TaskContract, documents[case.task_ref])
    policy = parse_model(Policy, documents[case.evaluation_policy_ref])
    environment = parse_model(Environment, documents[case.environment_ref])
    state = parse_model(InitialState, documents[case.initial_state_ref])
    provenance = parse_model(Provenance, documents[case.provenance_ref])
    if task.provenance_ref != case.provenance_ref or policy.provenance_ref != case.provenance_ref:
        raise InputError("PROVENANCE_REFERENCE_MISMATCH")
    return LoadedCase(
        root, case, manifest, task, policy, environment, state, provenance, raw, documents
    )


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise InputError("WORKFLOW_OBJECT_REQUIRED")
    return value
