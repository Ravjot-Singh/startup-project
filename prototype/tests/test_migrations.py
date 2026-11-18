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


def test_0002_turns_old_payments_into_receipts(tmp_path):
    """A database at revision 0001 with a manual payment and an upload-set paid amount upgrades cleanly."""
    from alembic import command
    from sqlalchemy import text

    from app.db import alembic_config

    url = _url(tmp_path)
    eng = _reset(url)
    try:
        command.upgrade(alembic_config(url), "0001")
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO customers (id, name, whatsapp_opt_in, reminders_paused, credit_days, is_active) "
                              "VALUES (1, 'A', true, false, 15, true)"))
            conn.execute(text("INSERT INTO invoices (id, customer_id, invoice_no, invoice_date, due_date, amount, amount_paid, status) "
                              "VALUES (1, 1, '101', '2026-03-20', '2026-04-04', 1000, 300, 'partial')"))
            conn.execute(text("INSERT INTO payments (id, invoice_id, amount, paid_on, mode, note) VALUES (1, 1, 100, '2026-04-01', 'upi', 'x')"))
        migrate(url)
        with eng.connect() as conn:
            receipts = conn.execute(text("SELECT source, amount, mode FROM receipts ORDER BY source")).all()
            payments = conn.execute(text("SELECT invoice_id, amount FROM payments ORDER BY amount")).all()
            fy = conn.execute(text("SELECT fiscal_year FROM invoices")).scalar_one()
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert [(s, float(a), m) for s, a, m in receipts] == [("manual", 100.0, "upi"), ("opening", 200.0, "busy")]
        assert [(i, float(a)) for i, a in payments] == [(1, 100.0), (1, 200.0)]
        assert fy == "2025-26"
        assert diff == []
    finally:
        _reset(url).dispose()
        eng.dispose()
