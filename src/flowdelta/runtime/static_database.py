"""정적 검사 실행 결과의 PostgreSQL 저장소 (knowledge extra 필요).

규칙은 분석 저장소(#73)와 같다. 실행 ID마다 내용이 하나만 있고, 행은 바꾸거나 지우지 않는다.
읽을 때마다 저장된 해시를 확인하고, 결과는 plan_id와 scan_run_id로만 찾는다.
정적 검사 결과 자체는 실행 상태와 탐지 결과를 담을 뿐 보안 판정(outcome)은 아니다.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from flowdelta.ingestion import InputError, digest, json_bytes, parse_json, parse_model
from flowdelta.knowledge.database import engine_for
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.runtime.static_scan import StaticScanRun

_INSERT = text("""
    INSERT INTO static.scan_run (
        scan_run_id, plan_id, case_id, snapshot_id, side, status, error_code,
        workflow_sha256, sha256, payload
    )
    VALUES (
        :scan_run_id, :plan_id, :case_id, :snapshot_id, :side, :status, :error_code,
        :workflow_sha256, :sha256, :payload
    )
    ON CONFLICT (scan_run_id) DO NOTHING
    RETURNING scan_run_id
""")
_SELECT_ONE = text("SELECT payload, sha256 FROM static.scan_run WHERE scan_run_id = :id")
_SELECT_BY_PLAN = text("""
    SELECT scan_run_id, payload, sha256 FROM static.scan_run
    WHERE plan_id = :plan_id
    ORDER BY side, scan_run_id
""")


def migrate_static_runs(settings: KnowledgeSettings) -> None:
    """정적 검사 전용 migration을 migration 권한으로 적용한다. 앱 역할은 읽기와 추가만 한다."""
    engine = engine_for(settings, migration=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except (CommandError, OSError, SQLAlchemyError):
        raise InputError("STATIC_MIGRATION_FAILED") from None
    finally:
        engine.dispose()


def encode_run(run: StaticScanRun) -> bytes:
    payload = json_bytes(run.model_dump(mode="json"))
    # 읽어 올 수 없는 내용은 저장하지 않는다.
    decode_run(payload, run.run_id)
    return payload


def decode_run(payload: bytes, run_id: str) -> StaticScanRun:
    run = parse_model(StaticScanRun, parse_json(payload))
    if run.run_id != run_id:
        raise InputError("STATIC_RUN_INTEGRITY_MISMATCH", location=run_id)
    return run


def _verified(run_id: str, payload: bytes, checksum: str) -> StaticScanRun:
    if digest(payload) != checksum:
        raise InputError("STATIC_RUN_INTEGRITY_MISMATCH", location=run_id)
    return decode_run(payload, run_id)


class DatabaseStaticRunRepository:
    """`static.scan_run` 행. 저장한 바이트 그대로 보관하고 읽을 때마다 해시를 확인한다."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, run: StaticScanRun) -> None:
        payload = encode_run(run)
        checksum = digest(payload)
        values = {
            "scan_run_id": run.run_id,
            "plan_id": run.plan_id,
            "case_id": run.case_id,
            "snapshot_id": run.snapshot_id,
            "side": run.side,
            "status": run.status,
            "error_code": run.error_code,
            "workflow_sha256": run.workflow_sha256,
            "sha256": checksum,
            "payload": payload,
        }
        try:
            with self._engine.begin() as connection:
                if connection.execute(_INSERT, values).first() is None:
                    # 같은 ID의 재저장은 같은 바이트일 때만 허용한다.
                    stored = connection.execute(_SELECT_ONE, {"id": run.run_id}).one()
                    stored_payload = bytes(stored.payload)
                    _verified(run.run_id, stored_payload, stored.sha256)
                    if stored_payload != payload:
                        raise InputError("STATIC_RUN_ID_CONFLICT", location=run.run_id)
        except SQLAlchemyError:
            raise InputError("STATIC_DATABASE_UNAVAILABLE") from None

    def get(self, scan_run_id: str) -> StaticScanRun:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(_SELECT_ONE, {"id": scan_run_id}).first()
        except SQLAlchemyError:
            raise InputError("STATIC_DATABASE_UNAVAILABLE") from None
        if row is None:
            raise InputError("STATIC_RUN_NOT_FOUND", location=scan_run_id)
        return _verified(scan_run_id, bytes(row.payload), row.sha256)

    def list_by_plan(
        self, plan_id: str, comparison_run_id: str | None = None
    ) -> list[StaticScanRun]:
        """정확한 plan_id의 실행만 반환한다. 같은 사례의 '최신 결과'를 고르지 않는다.

        comparison_run_id를 주면 같은 실행 묶음의 결과만 남긴다.
        """
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_BY_PLAN, {"plan_id": plan_id}).all()
        except SQLAlchemyError:
            raise InputError("STATIC_DATABASE_UNAVAILABLE") from None
        runs = []
        for row in rows:
            run = _verified(row.scan_run_id, bytes(row.payload), row.sha256)
            if run.plan_id != plan_id:
                # 조회 열과 내용이 다른 행은 잘못된 적재이므로 다른 계획의 결과로 돌려주지 않는다.
                raise InputError("STATIC_RUN_INTEGRITY_MISMATCH", location=row.scan_run_id)
            runs.append(run)
        if comparison_run_id is None:
            return runs
        return [run for run in runs if run.comparison_run_id == comparison_run_id]
