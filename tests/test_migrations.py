"""The Alembic migrations must build exactly the schema the models describe."""

import os

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.db import Base, make_engine, migrate


def _url(tmp_path):
    return os.getenv("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'migr.db').as_posix()}"


def _reset(url):
    eng = make_engine(url)
    Base.metadata.drop_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    return eng


def test_upgrade_from_empty_matches_models(tmp_path):
    url = _url(tmp_path)
    eng = _reset(url)
    try:
        migrate(url)
        with eng.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"models and migrations differ; run 'alembic revision --autogenerate': {diff}"
    finally:
        _reset(url).dispose()
        eng.dispose()


def test_old_database_without_alembic_is_adopted(tmp_path):
    url = _url(tmp_path)
    eng = _reset(url)
    try:
        # A database from before migrations existed: made by create_all, without the newer settings table
        tables = [t for name, t in Base.metadata.tables.items() if name != "app_settings"]
        Base.metadata.create_all(eng, tables=tables)
        migrate(url)
        assert {"app_settings", "alembic_version", "customers"} <= set(inspect(eng).get_table_names())
        migrate(url)  # the next start is a plain upgrade with nothing to do
    finally:
        _reset(url).dispose()
        eng.dispose()
