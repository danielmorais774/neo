import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

# Import all models so SQLModel.metadata knows about them
from db.models.user import User  # noqa: F401
from db.models.user_message import UserMessage  # noqa: F401
from db.models.conversation import ConversationSession, ConversationMessage  # noqa: F401
from db.models.user_preference import UserPreference  # noqa: F401
from db.models.pks import (  # noqa: F401
    PKSAuditLog,
    PKSCollection,
    PKSEntity,
    PKSEntityField,
    PKSEntityRelation,
    PKSSpace,
    PKSWorkspace,
    PKSWorkspaceLink,
)
from db.models.dataset import (  # noqa: F401
    DatasetAudit,
    DatasetDefinition,
    SkillInstall,
)

config = context.config

# Override sqlalchemy.url from env var if available
database_url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "")
# Alembic uses sync driver — replace asyncpg with psycopg2 if needed
if "asyncpg" in database_url:
    database_url = database_url.replace("postgresql+asyncpg", "postgresql")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

# Schema dos cadastros generativos (docs/datasets-crud-plan.md §9.4). As tabelas
# de lá nascem em RUNTIME (DDL do instalador) e por isso não estão no
# SQLModel.metadata — sem este filtro, o primeiro `alembic revision
# --autogenerate` proporia DROP TABLE para todo cadastro do usuário.
DATA_SCHEMA = "neo_data"


def include_object(obj, name, type_, reflected, compare_to):
    """Mantém o autogenerate cego para tudo que mora em ``neo_data``."""
    if getattr(obj, "schema", None) == DATA_SCHEMA:
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            include_schemas=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
