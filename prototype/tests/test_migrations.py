"""The Alembic migrations must build exactly the schema the models describe."""

import os

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.db import Base, make_engine, migrate
from conftest import drop_everything


def _url(tmp_path):
    return os.getenv("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'migr.db').as_posix()}"


def _reset(url):
    eng = make_engine(url)
    drop_everything(eng)  # also old tables (reminder_logs) and alembic_version
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
            # no explicit id: on Postgres that would leave the id counter at 1 and the migration's own insert would clash
            conn.execute(text("INSERT INTO payments (invoice_id, amount, paid_on, mode, note) VALUES (1, 100, '2026-04-01', 'upi', 'x')"))
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


def test_0003_moves_old_reminder_log_into_outbox_without_resending(tmp_path):
    """Old reminder_logs rows become messages, and steps they covered are marked done."""
    from datetime import date, timedelta

    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.db import alembic_config
    from app.reminders.engine import plan_reminders

    url = _url(tmp_path)
    eng = _reset(url)
    today = date.today()
    try:
        command.upgrade(alembic_config(url), "0002")
        with eng.begin() as conn:
            for rid, offset, template in ((1, 0, "payment_due_today"), (2, 30, "payment_final_notice")):
                conn.execute(text("INSERT INTO reminder_rules (id, name, offset_days, template_name, notify_salesman, "
                                  "escalate_to_owner, is_active) VALUES (:i, 'r', :o, :t, false, false, true)"),
                             {"i": rid, "o": offset, "t": template})
            conn.execute(text("INSERT INTO customers (id, name, phone, whatsapp_opt_in, reminders_paused, credit_days, is_active) "
                              "VALUES (1, 'A', '919800000001', true, false, 15, true)"))
            # OLD is 31 days late today; NEW is due today. Yesterday's run sent the 30-day notice covering OLD.
            for iid, no, due in ((1, "OLD", today - timedelta(days=31)), (2, "NEW", today)):
                conn.execute(text("INSERT INTO invoices (id, customer_id, invoice_no, fiscal_year, invoice_date, due_date, "
                                  "amount, amount_paid, status) VALUES (:i, 1, :n, '2026-27', :d, :d, 1000, 0, 'open')"),
                             {"i": iid, "n": no, "d": due})
            conn.execute(text("INSERT INTO reminder_logs (id, customer_id, rule_id, run_date, invoice_nos, amount_due, "
                              "message_preview, status, attempts) VALUES (7, 1, 2, :d, 'OLD', 1000, 'hi', 'delivered', 1)"),
                         {"d": today - timedelta(days=1)})
        migrate(url)
        with eng.connect() as conn:
            msg = conn.execute(text("SELECT id, kind, status, template_name, idempotency_key FROM messages")).one()
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert tuple(msg) == (7, "reminder", "delivered", "payment_final_notice", "legacy:7")
        assert diff == []
        with Session(eng) as s:
            due = {(i.invoice_no, r.offset_days) for p in plan_reminders(s, today) for i, r in p.steps}
        assert due == {("NEW", 0)}  # OLD's 30-day step already went out before the upgrade
    finally:
        _reset(url).dispose()
        eng.dispose()
