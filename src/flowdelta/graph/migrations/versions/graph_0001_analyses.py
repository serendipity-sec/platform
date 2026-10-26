"""Immutable A analyses: one stored content per analysis ID, never updated or deleted."""

from alembic import op

revision = "graph_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA graph")
    # Case and snapshot IDs are contract IDs; shared case/snapshot tables do not exist yet.
    op.execute("""
        CREATE TABLE graph.analysis (
            analysis_id text PRIMARY KEY
                CHECK (analysis_id ~ '^[A-Za-z0-9_.-]{1,128}$'
                       AND analysis_id NOT IN ('.', '..')),
            case_id text NOT NULL,
            before_snapshot_id text NOT NULL,
            after_snapshot_id text NOT NULL,
            producer text NOT NULL,
            origin text NOT NULL,
            sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            payload bytea NOT NULL,
            limitations jsonb NOT NULL CHECK (jsonb_typeof(limitations) = 'array'),
            extensions jsonb NOT NULL CHECK (jsonb_typeof(extensions) = 'object'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX analysis_case_snapshots
            ON graph.analysis (case_id, before_snapshot_id, after_snapshot_id)
    """)
    op.execute("""
        CREATE FUNCTION graph.reject_analysis_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'graph.analysis rows are immutable';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER analysis_rows_immutable
            BEFORE UPDATE OR DELETE ON graph.analysis
            FOR EACH ROW EXECUTE FUNCTION graph.reject_analysis_change()
    """)
    op.execute("""
        CREATE TRIGGER analysis_table_immutable
            BEFORE TRUNCATE ON graph.analysis
            FOR EACH STATEMENT EXECUTE FUNCTION graph.reject_analysis_change()
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='flowdelta_app') THEN
                RAISE EXCEPTION 'Required PostgreSQL role flowdelta_app does not exist';
            END IF;
            GRANT USAGE ON SCHEMA graph TO flowdelta_app;
            GRANT SELECT, INSERT ON graph.analysis TO flowdelta_app;
            GRANT SELECT ON public.alembic_version_graph TO flowdelta_app;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
