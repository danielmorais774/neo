"""add_fts_and_hnsw_indexes

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2025-01-01 00:00:00.000000

Adds:
- content_tsv (tsvector) column on conversation_message for full-text search
- Auto-update trigger for content_tsv (Portuguese + English)
- GIN index on content_tsv
- HNSW indexes on embedding columns for fast KNN
- token_count column on conversation_message
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6g7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── token_count column ────────────────────────────────────────
    op.execute(
        "ALTER TABLE conversation_message "
        "ADD COLUMN IF NOT EXISTS token_count INTEGER"
    )

    # ── tsvector column for full-text search ──────────────────────
    op.execute(
        "ALTER TABLE conversation_message "
        "ADD COLUMN IF NOT EXISTS content_tsv tsvector"
    )

    # ── Backfill existing rows ────────────────────────────────────
    op.execute(
        """
        UPDATE conversation_message
        SET content_tsv = (
            setweight(to_tsvector('portuguese', coalesce(content, '')), 'A') ||
            setweight(to_tsvector('english',    coalesce(content, '')), 'B')
        )
        WHERE content IS NOT NULL
        """
    )

    # ── Auto-update trigger ───────────────────────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION conversation_message_tsv_trigger()
        RETURNS trigger AS $$
        BEGIN
            NEW.content_tsv :=
                setweight(to_tsvector('portuguese', coalesce(NEW.content, '')), 'A') ||
                setweight(to_tsvector('english',    coalesce(NEW.content, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_conversation_message_tsv ON conversation_message
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_conversation_message_tsv
        BEFORE INSERT OR UPDATE OF content
        ON conversation_message
        FOR EACH ROW
        EXECUTE FUNCTION conversation_message_tsv_trigger()
        """
    )

    # ── GIN index on tsvector ─────────────────────────────────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_content_tsv "
        "ON conversation_message USING GIN (content_tsv)"
    )

    # ── HNSW indexes for fast KNN on embeddings ───────────────────
    # conversation_message.embedding (1536-dim)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_embedding_hnsw "
        "ON conversation_message USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # conversation_session.topic_embedding (1536-dim)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_session_topic_embedding_hnsw "
        "ON conversation_session USING hnsw (topic_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    # ── Remove HNSW indexes ───────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_conversation_session_topic_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_embedding_hnsw")

    # ── Remove GIN index ──────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_content_tsv")

    # ── Remove trigger + function ─────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS trg_conversation_message_tsv ON conversation_message")
    op.execute("DROP FUNCTION IF EXISTS conversation_message_tsv_trigger()")

    # ── Remove columns ────────────────────────────────────────────
    op.execute("ALTER TABLE conversation_message DROP COLUMN IF EXISTS content_tsv")
    op.drop_column("conversation_message", "token_count")
