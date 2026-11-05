"""Publish explicitly selected producer bytes with a manifest as the final commit marker."""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from pydantic import JsonValue, ValidationError

from flowdelta.contracts import ArtifactRef, Manifest, validate_relative_path
from flowdelta.ingestion import (
    MAX_TOTAL_BYTES,
    InputError,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.pipeline_io import read_handoff, read_manifest


def _references(value: JsonValue) -> Iterator[ArtifactRef]:
    """ArtifactRef-shaped objects retain their producer IDs, paths and hashes."""
    if isinstance(value, dict):
        if {"artifact_id", "path", "sha256", "size_bytes"}.issubset(value):
            yield parse_file_model(ArtifactRef, value)
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _unlinked(path: Path) -> Path:
    path = path.absolute()
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise InputError("SYMLINK_NOT_ALLOWED")
    return path


def _relative(path: str) -> str:
    try:
        validate_relative_path(path)
    except ValueError as exc:
        raise InputError("ARTIFACT_PATH_INVALID", location=path) from exc
    if path.casefold() == "manifest.json" or path.split("/")[0].casefold() == "input":
        raise InputError("RESERVED_ARTIFACT_PATH", location=path)
    if "private" in {part.casefold() for part in path.split("/")}:
        raise InputError("PRIVATE_ARTIFACT_FORBIDDEN", location=path)
    return path


def write_module_bundle(
    handoff_dir: Path, artifact_sources: dict[str, Path], output: Path
) -> Manifest:
    """Copy only manifest-listed input and explicitly supplied JSON files, without rewriting.

    This is a file/integrity operation, not an analysis, execution or security verdict.
    Partial A-only and CONTRACT_EXAMPLE bundles are allowed. Consumers determine readiness.
    """
    try:
        source = _unlinked(handoff_dir).resolve(strict=True)
        target = _unlinked(output)
        if target.exists():
            raise InputError("OUTPUT_ALREADY_EXISTS")
        if not target.parent.is_dir():
            raise InputError("OUTPUT_PARENT_MISSING")
        target = target.parent.resolve(strict=True) / target.name
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise InputError("OUTPUT_OVERLAPS_INPUT")

        read_handoff(source)
        input_manifest, _ = read_manifest(source)
        data = {"input/manifest.json": _read_local(source, "manifest.json")}
        declared = {}
        for ref in input_manifest.artifacts:
            relative = f"input/{ref.path}"
            data[relative] = _read_local(source, ref.path)
            declared[relative] = ref.model_copy(update={"path": relative})
        folded = {name.casefold() for name in data}
        for name, path in sorted(artifact_sources.items()):
            _relative(name)
            if name.casefold() in folded or any(
                name.casefold().startswith(other + "/") or other.startswith(name.casefold() + "/")
                for other in folded
            ):
                raise InputError("ARTIFACT_PATH_COLLISION", location=name)
            path = _unlinked(path)
            # Scope privacy to the producer/output workspace, not unrelated ancestors.
            try:
                producer_root = Path(os.path.commonpath((path.parent, target.parent)))
            except ValueError:  # Different Windows drives have no shared ancestor.
                producer_root = Path(path.anchor)
            if "private" in {part.casefold() for part in path.relative_to(producer_root).parts}:
                raise InputError("PRIVATE_ARTIFACT_FORBIDDEN", location=name)
            if not path.is_file():
                raise InputError("ARTIFACT_SOURCE_MISSING", location=name)
            data[name] = _read_local(path.parent, path.name)
            folded.add(name.casefold())
        if len(data) > 64:
            raise InputError("BUNDLE_FILE_LIMIT")
        if sum(map(len, data.values())) > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")

        # Only producer references use the outer root. Input references were validated
        # by read_handoff in their own root; never rewrite their bytes to add a prefix.
        for name in artifact_sources:
            for ref in _references(parse_json(data[name])):
                if ref.path not in data:
                    raise InputError("ARTIFACT_SOURCE_MISSING", location=ref.path)
                if ref.media_type != "application/json":
                    raise InputError("JSON_SOURCE_REQUIRED", location=ref.path)
                if (len(data[ref.path]), digest(data[ref.path])) != (ref.size_bytes, ref.sha256):
                    raise InputError("ARTIFACT_INTEGRITY_MISMATCH", location=ref.path)
                if ref.path in declared and ref != declared[ref.path]:
                    raise InputError("ARTIFACT_REFERENCE_CONFLICT", location=ref.path)
                declared[ref.path] = ref

        refs = []
        for name, content in sorted(data.items()):
            parse_json(content)
            if name in declared and (len(content), digest(content)) != (
                declared[name].size_bytes,
                declared[name].sha256,
            ):
                raise InputError("ARTIFACT_INTEGRITY_MISMATCH", location=name)
            refs.append(
                declared.get(name)
                or ArtifactRef(
                    artifact_id=f"bundle-{digest(name.encode())[:24]}",
                    path=name,
                    sha256=digest(content),
                    size_bytes=len(content),
                    kind="generated",
                )
            )
        try:
            manifest = Manifest(schema_version="0.4.0", artifacts=refs)
        except ValidationError as exc:
            raise InputError("BUNDLE_MANIFEST_INVALID") from exc

        # Stage beside the destination. Reserve without replacement; publish manifest last.
        with tempfile.TemporaryDirectory(prefix=".flowdelta-bundle-", dir=target.parent) as temp:
            stage = Path(temp)
            for name, content in data.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            (stage / "manifest.json").write_bytes(json_bytes(manifest.model_dump(mode="json")))
            target.mkdir(exist_ok=False)
            for entry in sorted(
                stage.iterdir(), key=lambda item: (item.name == "manifest.json", item.name)
            ):
                entry.rename(target / entry.name)
        return manifest
    except FileExistsError as exc:
        raise InputError("OUTPUT_ALREADY_EXISTS") from exc
    except FileNotFoundError as exc:
        raise InputError("ARTIFACT_SOURCE_MISSING") from exc
    except OSError as exc:
        raise InputError("ARTIFACT_WRITE_ERROR") from exc
