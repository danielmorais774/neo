"""Inscrições de evento (docs/playlists-skill-plan.md §7).

Duas tabelas de CONTROLE, no `public` e gerenciadas pelo alembic:

- ``event_subscription`` — quem quer ser avisado de quê, **em qual alvo
  concreto**. Uma inscrição nunca é "me avise quando qualquer música acabar":
  ela nasce grudada no aparelho, na aba e na faixa que o próprio Neo despachou
  (``expected_ref``), e é re-armada a cada despacho. É esse detalhe que impede
  um "toca um AC/DC" solto de acionar fila nenhuma.
- ``event_delivery`` — o que chegou, o que rodou e o que saiu. Sem esta trilha o
  modo de falha é "a fila parou e ninguém sabe por quê" — a mesma dor da skill
  HA dizendo "desliguei" sem ter desligado.

O QUE a inscrição faz não mora aqui: mora no ``eventos:`` do SKILL.md, e é
resolvido na hora de disparar (``handler_ref`` = "<skill>.<nome do gatilho>").
Assim editar o SKILL.md corrige uma fila em andamento, e uma skill removida
encerra a inscrição em vez de disparar lixo.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, Column, Integer, String, Text, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: Estados de :class:`EventSubscription`.
#:   armada    — vale e dispara.
#:   pausada   — o alvo saiu de baixo dela (o usuário assumiu o player). Não
#:               dispara, mas volta com uma frase ("continua a fila").
#:   encerrada — acabou (fila vazia, TTL, teto de disparos, kill switch).
SUBSCRIPTION_STATES = ("armada", "pausada", "encerrada")

#: Teto de segurança de toda inscrição, em minutos. Uma inscrição imortal é o
#: que mais assombra num sistema que a gente esquece que ligou.
DEFAULT_TTL_MINUTES = 120
DEFAULT_MAX_FIRES = 200


class EventSubscription(SQLModel, table=True):
    """Um contrato concreto: neste aparelho, nesta aba, nesta faixa, faça isto."""

    __tablename__ = "event_subscription"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    # Aparelho que vai reportar o evento. Parte da chave de casamento: um evento
    # de outro device NUNCA acorda esta inscrição.
    device_id: str = Field(
        default="", sa_column=Column("device_id", String(64), nullable=False, server_default="", index=True)
    )
    # Superfície do evento ("musica"), não o produto. Genérico de propósito: o
    # dia em que "chegou email" ou "cron bateu" usarem este trilho, nada aqui
    # muda — é só não hardcodar "youtube".
    channel: str = Field(sa_column=Column("channel", String(32), nullable=False, index=True))
    # Nomes de evento aceitos por esta inscrição (ex.: faixa_terminando,
    # faixa_terminou). Lista porque o gatilho principal e a rede de segurança
    # são eventos diferentes com a MESMA reação.
    events: list[Any] = Field(
        default_factory=list,
        sa_column=Column("events", JSONB, nullable=False, server_default="[]"),
    )
    # O alvo: {"site": "youtube", "tab_id": 42}. Sem alvo concreto a inscrição
    # não é criada — é a regra que separa "estou tocando a fila NESTA aba" de
    # "fico de olho em música".
    target: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("target", JSONB, nullable=False, server_default="{}"),
    )
    # A faixa que EU despachei. O evento que não trouxer esta referência não é
    # meu: o usuário trocou de música na mão, e a inscrição se pausa em vez de
    # brigar pelo player.
    expected_ref: str = Field(
        default="", sa_column=Column("expected_ref", String(128), nullable=False, server_default="")
    )
    # "<skill>.<gatilho>" — resolvido no `eventos:` do SKILL.md a cada disparo.
    handler_ref: str = Field(
        sa_column=Column("handler_ref", String(96), nullable=False)
    )
    # Cópia do gatilho no momento da inscrição. Só é usada se o SKILL.md sumir
    # no meio da fila (aí a inscrição encerra com motivo legível); a receita
    # VÁLIDA é sempre a do arquivo.
    handler_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("handler_snapshot", JSONB, nullable=False, server_default="{}"),
    )
    state: str = Field(
        default="armada",
        sa_column=Column("state", String(16), nullable=False, server_default="armada", index=True),
    )
    # Por que parou — texto para humano, não código.
    reason: str = Field(
        default="", sa_column=Column("reason", Text, nullable=False, server_default="")
    )
    fires: int = Field(
        default=0, sa_column=Column("fires", Integer, nullable=False, server_default="0")
    )
    max_fires: int = Field(
        default=DEFAULT_MAX_FIRES,
        sa_column=Column("max_fires", Integer, nullable=False, server_default=str(DEFAULT_MAX_FIRES)),
    )
    consecutive_errors: int = Field(
        default=0,
        sa_column=Column("consecutive_errors", Integer, nullable=False, server_default="0"),
    )
    # Sessão que criou a inscrição — é para onde vai o aviso proativo quando a
    # fila acaba ou o disjuntor abre.
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True)
    )
    expires_at: Optional[datetime] = Field(default=None, nullable=True)
    last_event_at: Optional[datetime] = Field(default=None, nullable=True)
    # Código do despacho em curso — o mesmo que viaja até o aparelho e volta em
    # cada evento. É ele que identifica a LINHA da fila que está tocando (a
    # mesma música enfileirada duas vezes tem dois códigos).
    current_code: str = Field(
        default="", sa_column=Column("current_code", String(16), nullable=False, server_default="")
    )
    # Último `event_id` aceito. Dedup barato: o mesmo evento reenviado (reconnect
    # da extensão, alarme + listener juntos) não avança a fila duas vezes.
    last_event_key: str = Field(
        default="", sa_column=Column("last_event_key", String(160), nullable=False, server_default="")
    )
    enabled: bool = Field(
        default=True, sa_column=Column("enabled", Boolean, nullable=False, server_default="true")
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class EventDelivery(SQLModel, table=True):
    """Uma entrega: o evento que chegou, a inscrição que casou e o que saiu."""

    __tablename__ = "event_delivery"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    subscription_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("subscription_id", SAUuid, nullable=True, index=True)
    )
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    channel: str = Field(
        default="", sa_column=Column("channel", String(32), nullable=False, server_default="")
    )
    event: str = Field(
        default="", sa_column=Column("event", String(40), nullable=False, server_default="")
    )
    # Chave de dedup do evento (device+aba+faixa+janela de tempo).
    event_key: str = Field(
        default="", sa_column=Column("event_key", String(160), nullable=False, server_default="", index=True)
    )
    # ignorado | despachado | encerrado | erro — o que ESTA entrega causou.
    outcome: str = Field(
        default="", sa_column=Column("outcome", String(20), nullable=False, server_default="")
    )
    reason: str = Field(
        default="", sa_column=Column("reason", Text, nullable=False, server_default="")
    )
    # O que foi mandado de volta ao aparelho (sem segredo nenhum: é link e id).
    action: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("action", JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
