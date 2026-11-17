"""conversation_message_chunk: embedding por pedaço de mensagem longa

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-09-19 00:00:00.000000

O embedder local (`all-MiniLM-L6-v2`) tem `max_seq_length = 256` tokens e
trunca em SILÊNCIO — não há erro, warning ou marcador. Na prática o vetor de
uma mensagem representa só os ~1.000 primeiros caracteres dela.

Medido no corpus real: 133 de 4.526 mensagens embeddadas (2,9%) passam desse
limite, e são justamente as análises longas — o que o usuário pede de volta.
Caso concreto: a "Reanálise Completa" da sessão 7330e40c tem 12.811 chars com
a tabela pedida no offset 9.399; a busca vetorial por ela devolvia 83º lugar
de 2.304, atrás da versão antiga e superada do mesmo texto.

Fatiando em pedaços de 1.200 chars (300 de sobreposição), cada um cabe na
janela do modelo e a similaridade medida vai de 0,5457 para 0,7173 — à frente
do antigo 1º colocado (0,7071). Pedaços de 2.000 chars foram medidos e são
PIORES (0,6101): ainda estouram os 256 tokens.

Só mensagens longas são fatiadas; as curtas seguem com o embedding da própria
linha em `conversation_message.embedding`.
"""

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def _has_pgvector(conn) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # A coluna acompanha o tipo de `conversation_message.embedding`: onde o
    # pgvector não existe, o resto do sistema já cai para JSON.
    embedding_type = "vector(384)" if _has_pgvector(conn) else "json"

    conn.execute(sa.text(f"""
        CREATE TABLE IF NOT EXISTS conversation_message_chunk (
            id UUID PRIMARY KEY,
            message_id UUID NOT NULL
                REFERENCES conversation_message(id) ON DELETE CASCADE,
            session_id UUID NOT NULL
                REFERENCES conversation_session(id) ON DELETE CASCADE,
            user_id UUID REFERENCES "user"(id),
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding {embedding_type},
            created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
        )
    """))
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_conv_chunk_message_idx "
        "ON conversation_message_chunk (message_id, chunk_index)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_conv_chunk_session "
        "ON conversation_message_chunk (session_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_conv_chunk_user "
        "ON conversation_message_chunk (user_id)"
    ))
    # Sem índice vetorial: com ~1.500 pedaços previstos o seq scan é mais
    # rápido e exato que um ivfflat mal-calibrado. Vale revisitar na casa das
    # dezenas de milhares.


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DROP TABLE IF EXISTS conversation_message_chunk CASCADE")
    )
