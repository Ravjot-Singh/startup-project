from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.db import Base

cfg = context.config
if not cfg.get_main_option("sqlalchemy.url"):
    cfg.set_main_option("sqlalchemy.url", app_config.DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=cfg.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(cfg.get_section(cfg.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # render_as_batch lets ALTER TABLE style changes work on SQLite too
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
