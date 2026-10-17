"""Immutable raw revisions, current normalized records, and resumable sync state."""

from alembic import op

revision = "kb_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA knowledge")
    op.execute("""
        CREATE TABLE knowledge.source_revision (
            source text NOT NULL,
            external_id text NOT NULL,
            sha256 char(64) NOT NULL,
            raw_path text NOT NULL,
            size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
            retrieved_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, external_id, sha256)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.source_record (
            source text NOT NULL,
            external_id text NOT NULL,
            sha256 char(64) NOT NULL,
            modified_at timestamptz,
            document jsonb NOT NULL,
            PRIMARY KEY (source, external_id),
            FOREIGN KEY (source, external_id, sha256)
                REFERENCES knowledge.source_revision(source, external_id, sha256)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.sync_state (
            source text PRIMARY KEY,
            cursor jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETE', 'FAILED')),
            records_seen bigint NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            error_code text
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.sync_failure (
            source text NOT NULL,
            external_id text NOT NULL,
            sha256 char(64) NOT NULL,
            error_code text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source, external_id, sha256)
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname='flowdelta_app') THEN
                GRANT USAGE ON SCHEMA knowledge TO flowdelta_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA knowledge
                    TO flowdelta_app;
                GRANT SELECT ON public.alembic_version TO flowdelta_app;
                ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge
                    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO flowdelta_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
