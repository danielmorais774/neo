"""rename_usermessage_to_user_message

Revision ID: d9ac18e84d84
Revises: efcda5bdcec4
Create Date: 2026-02-13 19:58:14.933045

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd9ac18e84d84'
down_revision: Union[str, Sequence[str], None] = 'efcda5bdcec4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table('usermessage', 'user_message')


def downgrade() -> None:
    op.rename_table('user_message', 'usermessage')
