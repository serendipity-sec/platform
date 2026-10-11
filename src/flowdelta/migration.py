"""Explicit, new-directory conversion of the eight-file 0.3.0 input bundle."""

from pathlib import Path

from flowdelta.contracts import ArtifactRef, ComparisonCase, Manifest
from flowdelta.ingestion import (
    MAX_TOTAL_BYTES,
    InputError,
    _read_local,
    digest,
    json_bytes,
    load_case,
    parse_json,
    parse_model,
)


def migrate_case_03(source: Path, target: Path) -> Path:
    source = source.resolve(strict=True)
    target = target.absolute()
    if target.exists() or target.is_symlink():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    parent = target.parent.resolve(strict=True)
    target = parent / target.name
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise InputError("OUTPUT_OVERLAPS_INPUT")
    old_manifest = parse_json(_read_local(source, "manifest.json"))
    if old_manifest.get("schema_version") != "0.3.0":
        raise InputError("MIGRATION_REQUIRES_0_3_0")
    converted = dict(old_manifest, schema_version="0.4.0")
    refs = converted.get("artifacts")
    if not isinstance(refs, list):
        raise InputError("CONTRACT_INVALID")
    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            raise InputError("CONTRACT_INVALID")
        raw_ref["schema_version"] = "0.4.0"
    manifest = parse_model(Manifest, converted)
    raw = {}
    total = 0
    for ref in manifest.artifacts:
        if ref.kind != "source" or ref.media_type != "application/json":
            raise InputError("MIGRATION_SOURCE_JSON_REQUIRED")
        data = _read_local(source, ref.path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")
        if digest(data) != ref.sha256 or len(data) != ref.size_bytes:
            raise InputError("ARTIFACT_INTEGRITY_MISMATCH")
        raw[ref.path] = data
    if "case.json" not in raw:
        raise InputError("CASE_NOT_IN_MANIFEST")
    case_json = parse_json(raw["case.json"])
    if case_json.get("schema_version") != "0.3.0":
        raise InputError("MIGRATION_REQUIRES_0_3_0")
    case_json["schema_version"] = "0.4.0"
    obligations = case_json.get("obligations")
    if isinstance(obligations, list):
        for obligation in obligations:
            if isinstance(obligation, dict):
                obligation["schema_version"] = "0.4.0"
    case = parse_model(ComparisonCase, case_json)
    if case.dependency_manifest_ref or set(raw) != {"case.json", *case.references()}:
        raise InputError("MIGRATION_REQUIRES_LEGACY_FILE_SET")
    raw["case.json"] = json_bytes(case_json)
    for relative in case.references()[2:]:
        document = parse_json(raw[relative])
        if document.get("schema_version") != "0.3.0":
            raise InputError("MIGRATION_REQUIRES_0_3_0")
        document["schema_version"] = "0.4.0"
        rules = document.get("rules")
        if relative == case.evaluation_policy_ref and isinstance(rules, list):
            for rule in rules:
                if isinstance(rule, dict):
                    rule["schema_version"] = "0.4.0"
        raw[relative] = json_bytes(document)
    target.mkdir()
    for relative, data in raw.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    updated_refs = [
        ArtifactRef(
            artifact_id=ref.artifact_id,
            path=ref.path,
            sha256=digest(raw[ref.path]),
            size_bytes=len(raw[ref.path]),
            kind="source",
        )
        for ref in manifest.artifacts
    ]
    (target / "manifest.json").write_bytes(
        json_bytes(Manifest(schema_version="0.4.0", artifacts=updated_refs).model_dump(mode="json"))
    )
    # Never label migrated inputs as executed; source workflows stay byte-for-byte identical.
    load_case(target)
    return target
