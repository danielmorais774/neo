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


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
