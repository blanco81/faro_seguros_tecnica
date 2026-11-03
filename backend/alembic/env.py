from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.adapters.db.models import Base
from app.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# La URL de conexión llega por variables de entorno (nunca hardcodeada).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Modo offline: genera SQL sin conectarse a la base."""
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
    """Modo online: se conecta a la base y aplica las migraciones."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
