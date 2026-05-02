"""resize_embeddings_1536_to_384

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-04-21 00:00:00.000000

Altera colunas de embedding de vector(1536) para vector(384),
compatível com o modelo local all-MiniLM-L6-v2.

Para usar OpenAI novamente (1536 dims), execute o downgrade e
configure EMBEDDING_PROVIDER=openai no .env.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f6g7h8i9j0k1"
down_revision: Union[str, Sequence[str], None] = "e5f6g7h8i9j0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove índices HNSW antes de alterar o tipo da coluna
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_conversation_session_topic_embedding_hnsw")

    # Zera os embeddings existentes (incompatíveis com 384 dims)
    op.execute("UPDATE conversation_message SET embedding = NULL")
    op.execute("UPDATE conversation_session SET topic_embedding = NULL")

    # Altera as colunas para vector(384)
    op.execute(
        "ALTER TABLE conversation_message "
        "ALTER COLUMN embedding TYPE vector(384) USING NULL"
    )
    op.execute(
        "ALTER TABLE conversation_session "
        "ALTER COLUMN topic_embedding TYPE vector(384) USING NULL"
    )

    # Recria índices HNSW para vector(384)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_embedding_hnsw "
        "ON conversation_message USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_session_topic_embedding_hnsw "
        "ON conversation_session USING hnsw (topic_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_conversation_session_topic_embedding_hnsw")

    op.execute("UPDATE conversation_message SET embedding = NULL")
    op.execute("UPDATE conversation_session SET topic_embedding = NULL")

    op.execute(
        "ALTER TABLE conversation_message "
        "ALTER COLUMN embedding TYPE vector(1536) USING NULL"
    )
    op.execute(
        "ALTER TABLE conversation_session "
        "ALTER COLUMN topic_embedding TYPE vector(1536) USING NULL"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_embedding_hnsw "
        "ON conversation_message USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_session_topic_embedding_hnsw "
        "ON conversation_session USING hnsw (topic_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
