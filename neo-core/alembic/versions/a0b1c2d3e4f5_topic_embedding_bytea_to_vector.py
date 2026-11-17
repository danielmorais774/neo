"""topic_embedding: bytea -> vector(384)

Revision ID: a0b1c2d3e4f5
Revises: z9a0b1c2d3e4
Create Date: 2026-09-19 00:00:00.000000

`conversation_session.topic_embedding` estava como **bytea** em produção,
enquanto `conversation_message.embedding` é `vector(384)`. Toda migration
sempre criou a coluna como `vector` — a tabela em prod foi criada fora do
alembic (o `session_metadata` também diverge: é `jsonb`, e a migration pede
`json`), e o alembic foi carimbado por cima.

Efeito: `find_similar_sessions` sempre explodia com

    operator does not exist: bytea <=> vector

e o `except` devolvia lista vazia com um `logger.warning`. Ou seja, a busca por
SESSÃO (por sumário de tópico) nunca devolveu nada — nos dois caminhos que a
usam (`HybridMemorySearch.search` e `_vector_only_recovery`) — e ninguém via,
porque falhava em silêncio.

O dado é recuperável: o caminho de escrita grava
`json.dumps(vetor).encode("utf-8")`, que é exatamente o literal textual que o
pgvector aceita. Medido antes de escrever esta migration: as 90 linhas com
embedding convertem, todas com 384 dimensões.

Idempotente: se a coluna já for `vector`, não faz nada.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "z9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIMS = 384


def _column_type() -> str:
    """Tipo atual da coluna, para a migration ser no-op onde já está certo."""
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        """
        SELECT COALESCE(NULLIF(udt_name, ''), data_type)
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'conversation_session'
          AND column_name = 'topic_embedding'
        LIMIT 1
        """
    ).scalar()
    return str(row or "").lower()


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    tipo = _column_type()
    if tipo == "vector":
        return
    if tipo != "bytea":
        # Tipo inesperado: não adivinha conversão, só garante a coluna certa.
        # Zerar é seguro — topic_embedding é derivado de topic_summary e o
        # `summarize_session` regrava no próximo fechamento de sessão.
        op.execute("ALTER TABLE conversation_session DROP COLUMN IF EXISTS topic_embedding")
        op.execute(
            f"ALTER TABLE conversation_session ADD COLUMN topic_embedding vector({_DIMS})"
        )
    else:
        op.execute("DROP INDEX IF EXISTS ix_conversation_session_topic_embedding_hnsw")
        # Linha que não decodifica para um vetor de 384 dims viraria erro no
        # meio do ALTER e derrubaria a migration inteira. Zerar só ela é melhor
        # que perder as outras 90 — o embedding é derivado e volta sozinho.
        op.execute(
            f"""
            UPDATE conversation_session
            SET topic_embedding = NULL
            WHERE topic_embedding IS NOT NULL
              AND NOT (
                  convert_from(topic_embedding, 'UTF8') ~ '^\\s*\\[.*\\]\\s*$'
                  AND array_length(
                          string_to_array(
                              btrim(convert_from(topic_embedding, 'UTF8'), '[] '), ','
                          ), 1
                      ) = {_DIMS}
              )
            """
        )
        op.execute(
            f"""
            ALTER TABLE conversation_session
            ALTER COLUMN topic_embedding TYPE vector({_DIMS})
            USING CASE
                WHEN topic_embedding IS NULL THEN NULL
                ELSE convert_from(topic_embedding, 'UTF8')::vector
            END
            """
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_session_topic_embedding_hnsw "
        "ON conversation_session USING hnsw (topic_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    if _column_type() != "vector":
        return
    op.execute("DROP INDEX IF EXISTS ix_conversation_session_topic_embedding_hnsw")
    # Volta ao formato antigo SEM perder o dado: o texto do vetor é o mesmo que
    # o caminho de escrita bytea produzia.
    op.execute(
        """
        ALTER TABLE conversation_session
        ALTER COLUMN topic_embedding TYPE bytea
        USING CASE
            WHEN topic_embedding IS NULL THEN NULL
            ELSE convert_to(topic_embedding::text, 'UTF8')
        END
        """
    )
