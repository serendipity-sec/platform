"""Exact-ID PostgreSQL storage for B's bounded output artifacts."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import (
    MAX_FILE_BYTES,
    InputError,
    _read_local,
    digest,
    parse_file_model,
    parse_json,
)
from flowdelta.pipeline_contracts import RetrievalResult, TestPlan
from flowdelta.pipeline_io import verify_ref


@dataclass(frozen=True)
class BOutputBundle:
    plan: TestPlan
    retrieval: RetrievalResult
    kb_snapshot_json: bytes
    retrieval_json: bytes
    plan_json: bytes


# shortcut: 8 MiB per artifact; switch when shared artifact storage is provisioned.
def read_b_outputs(plan_path: Path) -> BOutputBundle:
    _reject_links(plan_path)
    if not plan_path.parent.is_dir():
        raise InputError("KNOWLEDGE_OUTPUT_DIRECTORY_INVALID")
    root = plan_path.parent.resolve(strict=True)
    plan_json = _read_local(root, plan_path.name)
    plan = parse_file_model(TestPlan, parse_json(plan_json))
    retrieval_ref = plan.retrieval_ref
    if retrieval_ref is None or retrieval_ref.path != "retrieval.json":
        raise InputError("KNOWLEDGE_OUTPUT_REFERENCE_INVALID")
    verify_ref(root, retrieval_ref)
    retrieval_json = _read_local(root, retrieval_ref.path)
    retrieval = parse_file_model(RetrievalResult, parse_json(retrieval_json))
    snapshot_ref = retrieval.kb_snapshot_ref
    if snapshot_ref.path != "kb-snapshot.json":
        raise InputError("KNOWLEDGE_OUTPUT_REFERENCE_INVALID")
    verify_ref(root, snapshot_ref)
    kb_snapshot_json = _read_local(root, snapshot_ref.path)
    return _validate_bundle(kb_snapshot_json, retrieval_json, plan_json)


def store_b_outputs(engine: Engine, plan_path: Path) -> BOutputBundle:
    bundle = read_b_outputs(plan_path)
    kb_sha = digest(bundle.kb_snapshot_json)
    retrieval_sha = digest(bundle.retrieval_json)
    plan_sha = digest(bundle.plan_json)
    try:
        with engine.begin() as connection:
            _insert_and_check(
                connection,
                """
                INSERT INTO knowledge.b_kb_snapshot (sha256, payload_size_bytes, payload_bytes)
                VALUES (:sha256, :payload_size_bytes, :payload_bytes)
                ON CONFLICT DO NOTHING
                """,
                """
                SELECT sha256, payload_size_bytes, payload_bytes FROM knowledge.b_kb_snapshot
                WHERE sha256=:sha256
                """,
                {
                    "sha256": kb_sha,
                    "payload_size_bytes": len(bundle.kb_snapshot_json),
                    "payload_bytes": bundle.kb_snapshot_json,
                },
                "sha256",
            )
            _insert_and_check(
                connection,
                """
                INSERT INTO knowledge.b_retrieval (
                    retrieval_id, analysis_id, kb_snapshot_sha256,
                    payload_sha256, payload_size_bytes, payload_bytes
                ) VALUES (
                    :retrieval_id, :analysis_id, :kb_snapshot_sha256,
                    :payload_sha256, :payload_size_bytes, :payload_bytes
                )
                ON CONFLICT DO NOTHING
                """,
                """
                SELECT retrieval_id, analysis_id, kb_snapshot_sha256,
                       payload_sha256, payload_size_bytes, payload_bytes
                FROM knowledge.b_retrieval WHERE retrieval_id=:retrieval_id
                """,
                {
                    "retrieval_id": bundle.retrieval.retrieval_id,
                    "analysis_id": bundle.retrieval.analysis_id,
                    "kb_snapshot_sha256": kb_sha,
                    "payload_sha256": retrieval_sha,
                    "payload_size_bytes": len(bundle.retrieval_json),
                    "payload_bytes": bundle.retrieval_json,
                },
                "retrieval_id",
            )
            _insert_and_check(
                connection,
                """
                INSERT INTO knowledge.b_test_plan (
                    plan_id, case_id, analysis_id, before_snapshot_id, after_snapshot_id,
                    retrieval_id, payload_sha256, payload_size_bytes, payload_bytes
                ) VALUES (
                    :plan_id, :case_id, :analysis_id, :before_snapshot_id, :after_snapshot_id,
                    :retrieval_id, :payload_sha256, :payload_size_bytes, :payload_bytes
                )
                ON CONFLICT DO NOTHING
                """,
                """
                SELECT plan_id, case_id, analysis_id, before_snapshot_id, after_snapshot_id,
                       retrieval_id, payload_sha256, payload_size_bytes, payload_bytes
                FROM knowledge.b_test_plan WHERE plan_id=:plan_id
                """,
                {
                    "plan_id": bundle.plan.plan_id,
                    "case_id": bundle.plan.case_id,
                    "analysis_id": bundle.plan.analysis_id,
                    "before_snapshot_id": bundle.plan.before_snapshot_id,
                    "after_snapshot_id": bundle.plan.after_snapshot_id,
                    "retrieval_id": bundle.retrieval.retrieval_id,
                    "payload_sha256": plan_sha,
                    "payload_size_bytes": len(bundle.plan_json),
                    "payload_bytes": bundle.plan_json,
                },
                "plan_id",
            )
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_OUTPUT_STORE_FAILED") from None
    return bundle


def load_b_outputs(engine: Engine, plan_id: str) -> BOutputBundle | None:
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                    SELECT
                        p.plan_id, p.case_id, p.analysis_id AS plan_analysis_id,
                        p.before_snapshot_id, p.after_snapshot_id,
                        p.retrieval_id AS plan_retrieval_id,
                        p.payload_sha256 AS plan_sha256,
                        p.payload_size_bytes AS plan_size_bytes, p.payload_bytes AS plan_content,
                        r.retrieval_id, r.analysis_id AS retrieval_analysis_id,
                        r.kb_snapshot_sha256, r.payload_sha256 AS retrieval_sha256,
                        r.payload_size_bytes AS retrieval_size_bytes,
                        r.payload_bytes AS retrieval_content,
                        k.sha256 AS snapshot_sha256,
                        k.payload_size_bytes AS snapshot_size_bytes,
                        k.payload_bytes AS snapshot_content
                    FROM knowledge.b_test_plan AS p
                    JOIN knowledge.b_retrieval AS r
                      ON r.retrieval_id=p.retrieval_id AND r.analysis_id=p.analysis_id
                    JOIN knowledge.b_kb_snapshot AS k ON k.sha256=r.kb_snapshot_sha256
                    WHERE p.plan_id=:plan_id
                """),
                    {"plan_id": plan_id},
                )
                .mappings()
                .one_or_none()
            )
    except SQLAlchemyError:
        raise InputError("KNOWLEDGE_OUTPUT_READ_FAILED") from None
    if row is None:
        return None

    kb_snapshot_json = _checked_payload(
        row["snapshot_content"], row["snapshot_size_bytes"], row["snapshot_sha256"]
    )
    retrieval_json = _checked_payload(
        row["retrieval_content"], row["retrieval_size_bytes"], row["retrieval_sha256"]
    )
    plan_json = _checked_payload(row["plan_content"], row["plan_size_bytes"], row["plan_sha256"])
    bundle = _validate_bundle(kb_snapshot_json, retrieval_json, plan_json)
    if (
        bundle.plan.plan_id != row["plan_id"]
        or bundle.plan.case_id != row["case_id"]
        or bundle.plan.analysis_id != row["plan_analysis_id"]
        or bundle.plan.before_snapshot_id != row["before_snapshot_id"]
        or bundle.plan.after_snapshot_id != row["after_snapshot_id"]
        or bundle.retrieval.retrieval_id != row["retrieval_id"]
        or bundle.retrieval.retrieval_id != row["plan_retrieval_id"]
        or bundle.retrieval.analysis_id != row["retrieval_analysis_id"]
        or bundle.retrieval.kb_snapshot_ref.sha256 != row["kb_snapshot_sha256"]
        or digest(kb_snapshot_json) != row["snapshot_sha256"]
    ):
        raise InputError("KNOWLEDGE_OUTPUT_INTEGRITY_FAILED")
    return bundle


def export_b_outputs(bundle: BOutputBundle, output: Path) -> None:
    _reject_links(output)
    if output.exists() or output.is_symlink():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise InputError("OUTPUT_PARENT_MISSING")
    target = output.parent.resolve(strict=True) / output.name
    if target.exists() or target.is_symlink():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    created: list[Path] = []
    created_directory = False
    try:
        target.mkdir()
        created_directory = True
        for name, content in (
            ("kb-snapshot.json", bundle.kb_snapshot_json),
            ("retrieval.json", bundle.retrieval_json),
            ("plan.json", bundle.plan_json),
        ):
            path = target / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(content)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        if created_directory:
            try:
                target.rmdir()
            except OSError:
                pass
        raise InputError("KNOWLEDGE_OUTPUT_EXPORT_FAILED") from None


def _insert_and_check(
    connection: Connection,
    insert_sql: str,
    select_sql: str,
    values: dict[str, str | int | bytes],
    conflict_key: str,
) -> None:
    connection.execute(text(insert_sql), values)
    row = (
        connection.execute(text(select_sql), {conflict_key: values[conflict_key]}).mappings().one()
    )
    if any(
        bytes(row[field]) != value if isinstance(value, bytes) else row[field] != value
        for field, value in values.items()
    ):
        raise InputError("KNOWLEDGE_OUTPUT_ID_CONFLICT")


def _checked_payload(value: bytes, size: int, checksum: str) -> bytes:
    content = bytes(value)
    if len(content) > MAX_FILE_BYTES or len(content) != size or digest(content) != checksum:
        raise InputError("KNOWLEDGE_OUTPUT_INTEGRITY_FAILED")
    return content


def _validate_bundle(
    kb_snapshot_json: bytes, retrieval_json: bytes, plan_json: bytes
) -> BOutputBundle:
    if any(
        len(content) > MAX_FILE_BYTES for content in (kb_snapshot_json, retrieval_json, plan_json)
    ):
        raise InputError("FILE_SIZE_LIMIT")
    kb = parse_json(kb_snapshot_json)
    if (
        kb.get("schema_version") != "0.4.0"
        or kb.get("hash_basis") != "canonical-json-v1"
        or not isinstance(kb.get("cards"), list)
    ):
        raise InputError("KNOWLEDGE_OUTPUT_INVALID")
    retrieval = parse_file_model(RetrievalResult, parse_json(retrieval_json))
    plan = parse_file_model(TestPlan, parse_json(plan_json))
    expected_plan_id = f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}"
    context = retrieval.query.get("analysis_context")
    if (
        plan.retrieval_ref is None
        or plan.plan_id != expected_plan_id
        or plan.analysis_id != retrieval.analysis_id
        or plan.retrieval_ref.artifact_id != retrieval.retrieval_id
        or plan.retrieval_ref.path != "retrieval.json"
        or plan.retrieval_ref.sha256 != digest(retrieval_json)
        or plan.retrieval_ref.size_bytes != len(retrieval_json)
        or retrieval.kb_snapshot_ref.artifact_id != "knowledge-snapshot"
        or retrieval.kb_snapshot_ref.path != "kb-snapshot.json"
        or retrieval.kb_snapshot_ref.sha256 != digest(kb_snapshot_json)
        or retrieval.kb_snapshot_ref.size_bytes != len(kb_snapshot_json)
        or ("analysis_context" in retrieval.query and not isinstance(context, dict))
        or (
            isinstance(context, dict)
            and any(
                context.get(key) != value
                for key, value in (
                    ("case_id", plan.case_id),
                    ("analysis_id", plan.analysis_id),
                    ("before_snapshot_id", plan.before_snapshot_id),
                    ("after_snapshot_id", plan.after_snapshot_id),
                )
            )
        )
    ):
        raise InputError("KNOWLEDGE_OUTPUT_INTEGRITY_FAILED")
    return BOutputBundle(plan, retrieval, kb_snapshot_json, retrieval_json, plan_json)


def _reject_links(path: Path) -> None:
    absolute = path.absolute()
    if any(part.is_symlink() or part.is_junction() for part in (absolute, *absolute.parents)):
        raise InputError("SYMLINK_NOT_ALLOWED")
