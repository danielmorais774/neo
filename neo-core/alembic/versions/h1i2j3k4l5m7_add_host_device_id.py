"""add_host_device_id_to_connected_device

Superfície de navegador (docs/browser-surface-plan.md §3): a extensão passa a ser
uma linha em ``connected_device``, e ``host_device_id`` é o vínculo com a MÁQUINA
onde ela roda — o que permite resolver "o navegador local do macbook" quando o
pedido vem do celular, sem desktop no canal.

Aditiva e anulável: linha existente continua com NULL, que significa "não é
superfície" (ou "é superfície pareada antes desta coluna existir"). Rollback =
dropar a coluna.

Revision ID: h1i2j3k4l5m7
Revises: g1h2i3j4k5l6
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "h1i2j3k4l5m7"
down_revision: Union[str, Sequence[str], None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELA = "connected_device"
_COLUNA = "host_device_id"
_INDICE = "ix_connected_device_host_device_id"


def _tem_coluna(bind) -> bool:
    return any(c["name"] == _COLUNA for c in inspect(bind).get_columns(_TABELA))


def upgrade() -> None:
    bind = op.get_bind()
    if _tem_coluna(bind):
        return
    op.add_column(_TABELA, sa.Column(_COLUNA, sa.String(length=256), nullable=True))
    # O acesso quente é "qual superfície roda naquela máquina" — a pergunta da
    # história do celular.
    op.create_index(_INDICE, _TABELA, [_COLUNA])


def downgrade() -> None:
    bind = op.get_bind()
    if not _tem_coluna(bind):
        return
    op.drop_index(_INDICE, table_name=_TABELA)
    op.drop_column(_TABELA, _COLUNA)
