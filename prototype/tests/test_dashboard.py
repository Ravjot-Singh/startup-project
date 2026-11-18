"""The Today page: numbers, 'Needs you', 'Going out today', and that the preview matches the real run."""

from datetime import datetime, timedelta
from decimal import Decimal

from app import settings
from app.dashboard import build_today
from app.models import Customer, ImportBatch, InboundMessage, Invoice, Payment, ReminderLog, ReminderRule
from app.reminders.engine import run_reminders
from app.whatsapp.provider import DryRunProvider

NOW = datetime(2026, 11, 18, 11, 0)
TODAY = NOW.date()


def cust(db, name, phone="919800000001", **kw):
    c = Customer(name=name, shop_name=name, phone=phone, **kw)
    db.add(c)
    db.flush()
    return c


def bill(db, c, no, days_overdue, amount=1000, paid=0):
    due = TODAY - timedelta(days=days_overdue)
    inv = Invoice(customer=c, invoice_no=no, invoice_date=due - timedelta(days=15), due_date=due,
                  amount=Decimal(amount), amount_paid=Decimal(paid))
    inv.refresh_status()
    db.add(inv)
    db.commit()
    return inv


def test_empty_database(db):
    v = build_today(db, NOW)
    assert not v.has_data and v.needs_count == 0 and v.outgoing == []
    assert v.due_total == v.overdue_total == v.collected_7d == 0


def test_money_numbers(db):
    a, b = cust(db, "A"), cust(db, "B", phone="919800000002")
    bill(db, a, "A1", days_overdue=10, amount=5000, paid=2000)  # 3000 overdue
    bill(db, a, "A2", days_overdue=-5, amount=1000)             # 1000 not yet due
    paid = bill(db, b, "B1", days_overdue=-2, amount=4000)
    db.add_all([
        Payment(invoice=paid, amount=Decimal("700"), paid_on=TODAY - timedelta(days=6)),
        Payment(invoice=paid, amount=Decimal("300"), paid_on=TODAY - timedelta(days=7)),  # outside last 7 days
    ])
    db.commit()
    v = build_today(db, NOW)
    assert v.due_total == Decimal("8000")
    assert v.overdue_total == Decimal("3000")
    assert v.collected_7d == Decimal("700")
    assert (v.customers_due, v.customers_overdue) == (2, 1)


def test_needs_you(db):
    a = cust(db, "Late Customer")
    bill(db, a, "L1", days_overdue=20)
    nophone = cust(db, "No Phone", phone=None)
    bill(db, nophone, "N1", days_overdue=2)
    cust(db, "No Phone No Dues", phone=None)
    rule = db.query(ReminderRule).first()
    db.add_all([
        InboundMessage(customer=a, phone=a.phone, text="Will pay Monday"),
        InboundMessage(customer=a, phone=a.phone, text="old", handled=True),
        ReminderLog(customer_id=a.id, rule_id=rule.id, run_date=TODAY - timedelta(days=1), invoice_nos="L1",
                    amount_due=Decimal(1000), message_preview="x", status="failed", error="131026: not on WhatsApp"),
        ReminderLog(customer_id=a.id, rule_id=rule.id, run_date=TODAY - timedelta(days=9), invoice_nos="L1",
                    amount_due=Decimal(1000), message_preview="x", status="failed", error="too old to show"),
    ])
    db.commit()
    v = build_today(db, NOW)
    assert [m.text for m in v.replies] == ["Will pay Monday"] and v.replies_count == 1
    assert [l.error for l in v.failed] == ["131026: not on WhatsApp"]
    assert v.to_call_count == 1 and v.to_call[0]["customer"].name == "Late Customer"
    assert v.no_phone_count == 1  # only the one who owes money
    assert v.needs_count == 4  # 1 reply + 1 failed + call-list row + no-phone row


def test_needs_count_matches_rows_not_customers(db):
    for i in range(3):
        bill(db, cust(db, f"Late {i}", phone=f"9198000002{i:02d}"), f"L{i}", days_overdue=20)
    v = build_today(db, NOW)
    assert v.to_call_count == 3 and v.needs_count == 1


def _states(v):
    return {o.plan.customer.name: (o.state, o.note) for o in v.outgoing}


def test_going_out_today_before_and_after_run(db):
    due = cust(db, "Due Today")
    bill(db, due, "D1", days_overdue=0)
    nophone = cust(db, "No Number", phone=None)
    bill(db, nophone, "X1", days_overdue=7)
    bill(db, cust(db, "Not Today", phone="919800000003"), "Z1", days_overdue=5)  # no rule at 5 days

    before = build_today(db, NOW.replace(hour=8))
    assert _states(before) == {"Due Today": ("waiting", "Sends at 10:00"), "No Number": ("skipped", "Won't send: no phone number")}

    run_reminders(db, DryRunProvider(), today=TODAY, now=NOW)
    after = build_today(db, NOW)
    assert _states(after) == {"Due Today": ("sent", "Sent (test mode)"), "No Number": ("skipped", "Not sent: no phone number")}


def test_going_out_today_paused_and_outside_hours(db):
    bill(db, cust(db, "Due Today"), "D1", days_overdue=0)
    assert _states(build_today(db, NOW.replace(hour=21)))["Due Today"][1].startswith("Outside sending hours")
    settings.set_value(db, "sending_paused", True)
    assert _states(build_today(db, NOW))["Due Today"] == ("paused", "Paused")


def test_preview_matches_what_the_run_sends(db):
    for i, offset in enumerate((-3, 0, 3, 7, 15, 30, 4, 9)):
        bill(db, cust(db, f"C{i}", phone=f"9198000001{i:02d}"), f"B{i}", days_overdue=offset)
    preview = {o.plan.customer.name: o.plan.rule.template_name for o in build_today(db, NOW).outgoing}
    p = DryRunProvider()
    run_reminders(db, p, today=TODAY, now=NOW)
    sent = {name: template for (_, template, (name, *_)) in p.sent}
    assert preview == sent and len(sent) == 6


def test_last_import_age(db):
    db.add(ImportBatch(file_name="busy.xlsx", created_at=NOW - timedelta(days=4)))
    db.commit()
    v = build_today(db, NOW)
    assert v.has_data and v.import_age_days == 4
