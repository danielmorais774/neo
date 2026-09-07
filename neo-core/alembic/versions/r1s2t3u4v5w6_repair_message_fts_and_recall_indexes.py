"""repair_message_fts_and_recall_indexes

Revision ID: r1s2t3u4v5w6
Revises: q0r1s2t3u4v5
Create Date: 2026-08-02 00:00:00.000000

Repara o FTS de `conversation_message` e adiciona os índices que o recall de
conversas (docs/conversation-recall-plan.md) precisa.

Por que uma revision NOVA em vez de rodar a antiga: a `b2c3d4e5f6g7` já está
carimbada como aplicada (o head está muito à frente dela), mas a coluna
`content_tsv` NUNCA existiu no banco de produção — a tabela nasceu de um
`SQLModel.metadata.create_all()`, que só cria o que está no model, e o alembic
foi carimbado por cima. Consequência medida: TODA chamada de
`search_messages_fts` caía no `except`, logava `FTS search failed` e devolvia
`[]` — a metade lexical da busca híbrida estava morta desde sempre.

Tudo aqui é idempotente (`IF NOT EXISTS`/`CREATE OR REPLACE`), então roda sem
dano num banco que por acaso já tenha a coluna.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "r1s2t3u4v5w6"
down_revision: Union[str, Sequence[str], None] = "q0r1s2t3u4v5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── pg_trgm: o trilho de typo do recall depende dele ──────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── tsvector + backfill ───────────────────────────────────────
    # Deliberadamente FORA do SQLModel: a coluna é mantida por trigger. Se
    # entrasse no model, um `create_all` num banco novo criaria a coluna sem o
    # trigger e a divergência voltaria pelo outro lado.
    op.execute(
        "ALTER TABLE conversation_message "
        "ADD COLUMN IF NOT EXISTS content_tsv tsvector"
    )
    op.execute(
        """
        UPDATE conversation_message
        SET content_tsv = (
            setweight(to_tsvector('portuguese', coalesce(content, '')), 'A') ||
            setweight(to_tsvector('english',    coalesce(content, '')), 'B')
        )
        WHERE content IS NOT NULL AND content_tsv IS NULL
        """
    )

    # ── trigger de manutenção ─────────────────────────────────────
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
        "DROP TRIGGER IF EXISTS trg_conversation_message_tsv ON conversation_message"
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

    # ── índices ───────────────────────────────────────────────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_content_tsv "
        "ON conversation_message USING GIN (content_tsv)"
    )
    # Trilho de typo: `word_similarity` (operador %>) sobre o texto minúsculo.
    # gin_trgm_ops porque a busca é "termo curto DENTRO de documento longo".
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversation_message_content_trgm "
        "ON conversation_message USING GIN (lower(content) gin_trgm_ops)"
    )
    # O recall filtra sempre por dono + janela temporal.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conv_msg_user_created "
        "ON conversation_message (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conv_msg_user_created")
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_content_trgm")
    op.execute("DROP INDEX IF EXISTS ix_conversation_message_content_tsv")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_conversation_message_tsv ON conversation_message"
    )
    op.execute("DROP FUNCTION IF EXISTS conversation_message_tsv_trigger()")
    op.execute("ALTER TABLE conversation_message DROP COLUMN IF EXISTS content_tsv")
