"""add_password_hash_to_user

Revision ID: 04cf18aa2e51
Revises: d9ac18e84d84
Create Date: 2026-02-13 20:25:23.766147

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '04cf18aa2e51'
down_revision: Union[str, Sequence[str], None] = 'd9ac18e84d84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user', sa.Column('password_hash', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('user', 'password_hash')
