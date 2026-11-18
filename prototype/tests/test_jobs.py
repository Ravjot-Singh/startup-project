"""The /jobs/tick endpoint the external cron calls, and the tick logic itself."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app import config, settings
from app.jobs import run_tick
from app.models import Customer, Invoice, Message
from app.reminders.engine import schedule_one_off
from app.whatsapp.provider import DryRunProvider

TOKEN = "t" * 32
DAY = datetime(2026, 11, 10)


def _due_today_customer(db):
    c = Customer(name="A", phone="919800000001")
    db.add(c)
    db.flush()
    inv = Invoice(customer=c, invoice_no="B1", invoice_date=DAY.date() - timedelta(days=15), due_date=DAY.date(),
                  amount=Decimal("1000"), amount_paid=Decimal("0"))
    inv.refresh_status()
    db.add(inv)
    db.commit()


def test_tick_endpoint_disabled_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "TICK_TOKEN", "")
    assert client.post("/jobs/tick", headers={"X-Tick-Token": "anything"}).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Tick-Token": "wrong"}, {"X-Tick-Token": TOKEN[:-1]}])
def test_tick_endpoint_rejects_bad_token(client, monkeypatch, headers):
    monkeypatch.setattr(config, "TICK_TOKEN", TOKEN)
    assert client.post("/jobs/tick", headers=headers).status_code == 403


def test_tick_endpoint_runs_with_right_token(client, monkeypatch):
    monkeypatch.setattr(config, "TICK_TOKEN", TOKEN)
    r = client.post("/jobs/tick", headers={"X-Tick-Token": TOKEN})
    assert r.status_code == 200 and "reminders" in r.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_tick_waits_until_run_hour(db):
    _due_today_customer(db)
    p = DryRunProvider()
    assert run_tick(db, p, now=DAY.replace(hour=8))["reminders"] == "waiting until 10:00"
    assert p.sent == []


def test_tick_sends_once_even_if_called_repeatedly(db):
    _due_today_customer(db)
    p = DryRunProvider()
    first = run_tick(db, p, now=DAY.replace(hour=10, minute=5))
    second = run_tick(db, p, now=DAY.replace(hour=10, minute=15))
    third = run_tick(db, p, now=DAY.replace(hour=14))
    assert first["sent"] == 1 and second["sent"] == 0 and third["sent"] == 0
    assert len(p.sent) == 1 and db.query(Message).count() == 1


def test_tick_respects_pause_switch(db):
    _due_today_customer(db)
    settings.set_value(db, "sending_paused", True)
    p = DryRunProvider()
    assert run_tick(db, p, now=DAY.replace(hour=11))["reminders"] == "paused"
    assert p.sent == []


def test_tick_uses_run_hour_from_settings(db):
    _due_today_customer(db)
    settings.set_value(db, "reminder_run_hour", 12)
    p = DryRunProvider()
    assert run_tick(db, p, now=DAY.replace(hour=11))["reminders"] == "waiting until 12:00"
    assert run_tick(db, p, now=DAY.replace(hour=12))["sent"] == 1


def test_tick_outside_send_window(db):
    _due_today_customer(db)
    settings.set_value(db, "send_window_end", 20)
    p = DryRunProvider()
    assert run_tick(db, p, now=DAY.replace(hour=21))["reminders"] == "outside send window"
    assert p.sent == []


def test_tick_before_run_hour_still_sends_a_due_one_off(db):
    _due_today_customer(db)
    c = db.query(Customer).one()
    schedule_one_off(db, c, DAY.replace(hour=9, minute=30), now=DAY.replace(hour=8))
    p = DryRunProvider()
    r = run_tick(db, p, now=DAY.replace(hour=9, minute=30))
    assert r["reminders"] == "waiting until 10:00" and r["sent"] == 1
    assert len(p.sent) == 1


def test_paused_holds_one_offs_too(db):
    _due_today_customer(db)
    c = db.query(Customer).one()
    schedule_one_off(db, c, DAY.replace(hour=11), now=DAY.replace(hour=8))
    settings.set_value(db, "sending_paused", True)
    p = DryRunProvider()
    run_tick(db, p, now=DAY.replace(hour=11, minute=5))
    assert p.sent == []
    settings.set_value(db, "sending_paused", False)
    run_tick(db, p, now=DAY.replace(hour=11, minute=15))
    assert len(p.sent) == 2  # the one-off plus the day's scheduled reminder
