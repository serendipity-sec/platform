"""Persist immutable B knowledge snapshots, retrievals, and plans."""

from alembic import op

revision = "kb_0003"
down_revision = "kb_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE knowledge.b_kb_snapshot (
            sha256              varchar(64) PRIMARY KEY
                                CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            payload_bytes       bytea NOT NULL,
            payload_size_bytes  bigint NOT NULL
                                CHECK (payload_size_bytes BETWEEN 1 AND 8388608),
            created_at          timestamptz NOT NULL DEFAULT now(),
            CHECK (octet_length(payload_bytes) = payload_size_bytes)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.b_retrieval (
            retrieval_id        text PRIMARY KEY CHECK (retrieval_id <> ''),
            analysis_id         text NOT NULL CHECK (analysis_id <> ''),
            kb_snapshot_sha256  varchar(64) NOT NULL,
            payload_bytes       bytea NOT NULL,
            payload_sha256      varchar(64) NOT NULL
                                CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            payload_size_bytes  bigint NOT NULL
                                CHECK (payload_size_bytes BETWEEN 1 AND 8388608),
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (retrieval_id, analysis_id),
            FOREIGN KEY (kb_snapshot_sha256)
                REFERENCES knowledge.b_kb_snapshot (sha256)
                ON DELETE RESTRICT,
            CHECK (octet_length(payload_bytes) = payload_size_bytes)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.b_test_plan (
            plan_id             text PRIMARY KEY CHECK (plan_id <> ''),
            retrieval_id        text NOT NULL CHECK (retrieval_id <> ''),
            analysis_id         text NOT NULL CHECK (analysis_id <> ''),
            case_id             text NOT NULL CHECK (case_id <> ''),
            before_snapshot_id  text NOT NULL CHECK (before_snapshot_id <> ''),
            after_snapshot_id   text NOT NULL CHECK (after_snapshot_id <> ''),
            payload_bytes       bytea NOT NULL,
            payload_sha256      varchar(64) NOT NULL
                                CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            payload_size_bytes  bigint NOT NULL
                                CHECK (payload_size_bytes BETWEEN 1 AND 8388608),
            created_at          timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (retrieval_id, analysis_id)
                REFERENCES knowledge.b_retrieval (retrieval_id, analysis_id)
                ON DELETE RESTRICT,
            CHECK (octet_length(payload_bytes) = payload_size_bytes)
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='flowdelta_app') THEN
                RAISE EXCEPTION 'Required PostgreSQL role flowdelta_app does not exist';
            END IF;
            GRANT SELECT, INSERT ON
                knowledge.b_kb_snapshot,
                knowledge.b_retrieval,
                knowledge.b_test_plan
                TO flowdelta_app;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
