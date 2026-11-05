"""정적 검사 실행 결과: 변경할 수 없는 행, 한 실행 ID당 내용 하나."""

from alembic import op

revision = "static_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA static")
    op.execute("""
        CREATE TABLE static.scan_run (
            scan_run_id text PRIMARY KEY
                CHECK (scan_run_id ~ '^[A-Za-z0-9_.-]{1,128}$'
                       AND scan_run_id NOT IN ('.', '..')),
            plan_id text NOT NULL,
            case_id text NOT NULL,
            snapshot_id text NOT NULL,
            side text NOT NULL CHECK (side IN ('before', 'after')),
            status text NOT NULL,
            error_code text,
            workflow_sha256 char(64) NOT NULL CHECK (workflow_sha256 ~ '^[0-9a-f]{64}$'),
            sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            payload bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX scan_run_plan ON static.scan_run (plan_id, side)")
    op.execute("CREATE INDEX scan_run_case ON static.scan_run (case_id)")
    op.execute("""
        CREATE FUNCTION static.reject_scan_run_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'static.scan_run rows are immutable';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER scan_run_rows_immutable
            BEFORE UPDATE OR DELETE ON static.scan_run
            FOR EACH ROW EXECUTE FUNCTION static.reject_scan_run_change()
    """)
    op.execute("""
        CREATE TRIGGER scan_run_table_immutable
            BEFORE TRUNCATE ON static.scan_run
            FOR EACH STATEMENT EXECUTE FUNCTION static.reject_scan_run_change()
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='flowdelta_app') THEN
                RAISE EXCEPTION 'Required PostgreSQL role flowdelta_app does not exist';
            END IF;
            GRANT USAGE ON SCHEMA static TO flowdelta_app;
            GRANT SELECT, INSERT ON static.scan_run TO flowdelta_app;
            GRANT SELECT ON public.alembic_version_static TO flowdelta_app;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
