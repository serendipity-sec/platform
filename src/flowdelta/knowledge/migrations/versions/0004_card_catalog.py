"""Persist immutable ScenarioCards and profile-scoped vector embeddings."""

from alembic import op

revision = "kb_0004"
down_revision = "kb_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE knowledge.b_scenario_card (
            card_id             text NOT NULL CHECK (card_id <> ''),
            card_version        text NOT NULL CHECK (card_version <> ''),
            content_sha256      varchar(64) NOT NULL
                                CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            payload_size_bytes  bigint NOT NULL
                                CHECK (payload_size_bytes BETWEEN 1 AND 8388608),
            payload_bytes       bytea NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (card_id, card_version),
            UNIQUE (card_id, card_version, content_sha256),
            CHECK (octet_length(payload_bytes) = payload_size_bytes)
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.b_embedding_profile (
            profile_id                  varchar(34) PRIMARY KEY
                                        CHECK (profile_id ~ '^embedding-[0-9a-f]{24}$'),
            provider                    text NOT NULL CHECK (provider <> ''),
            model                       text NOT NULL CHECK (model <> ''),
            model_digest                text NOT NULL CHECK (model_digest <> ''),
            dimensions                  integer NOT NULL CHECK (dimensions = 1024),
            query_instruction_version   text NOT NULL
                                        CHECK (query_instruction_version <> ''),
            normalized                  boolean NOT NULL,
            created_at                  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE knowledge.b_scenario_card_embedding (
            card_id         text NOT NULL,
            card_version    text NOT NULL,
            profile_id      varchar(34) NOT NULL,
            content_sha256  varchar(64) NOT NULL
                            CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            embedding       vector(1024) NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (card_id, card_version, profile_id),
            FOREIGN KEY (card_id, card_version, content_sha256)
                REFERENCES knowledge.b_scenario_card (card_id, card_version, content_sha256)
                ON DELETE RESTRICT,
            FOREIGN KEY (profile_id)
                REFERENCES knowledge.b_embedding_profile (profile_id)
                ON DELETE RESTRICT
        )
    """)
    op.execute("""
        CREATE INDEX b_scenario_card_embedding_hnsw_cosine_idx
            ON knowledge.b_scenario_card_embedding
            USING hnsw (embedding vector_cosine_ops)
    """)
    op.execute("""
        CREATE INDEX b_scenario_card_embedding_profile_idx
            ON knowledge.b_scenario_card_embedding (profile_id, card_id, card_version)
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='flowdelta_app') THEN
                RAISE EXCEPTION 'Required PostgreSQL role flowdelta_app does not exist';
            END IF;
            GRANT SELECT, INSERT ON
                knowledge.b_scenario_card,
                knowledge.b_embedding_profile,
                knowledge.b_scenario_card_embedding
                TO flowdelta_app;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; destructive downgrade is not automated")
