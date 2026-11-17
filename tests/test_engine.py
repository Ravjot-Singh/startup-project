from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import Customer, Invoice, Payment, ReminderLog
from app.reminders.engine import call_list, run_reminders
from app.whatsapp.provider import DryRunProvider, SendResult

TODAY = date(2026, 11, 10)
NOON = datetime(2026, 11, 10, 12, 0)


def make_customer(db, name="Sharma Store", phone="919800000001", **kw):
    c = Customer(name=name, shop_name=name, phone=phone, **kw)
    db.add(c)
    db.flush()
    return c


def make_invoice(db, customer, no, days_overdue, amount=1000, paid=0, status=None):
    due = TODAY - timedelta(days=days_overdue)
    inv = Invoice(customer=customer, invoice_no=no, invoice_date=due - timedelta(days=15), due_date=due,
                  amount=Decimal(amount), amount_paid=Decimal(paid))
    inv.refresh_status()
    if status:
        inv.status = status
    db.add(inv)
    db.commit()
    return inv


def run(db, provider=None, now=NOON):
    provider = provider or DryRunProvider()
    return run_reminders(db, provider, today=TODAY, now=now), provider


def test_sends_on_each_rule_offset(db):
    for offset in (-3, 0, 3, 7, 15, 30):
        c = make_customer(db, name=f"C{offset}", phone=f"9198000000{offset + 50:02d}")
        make_invoice(db, c, f"INV{offset}", offset)
    summary, provider = run(db)
    assert summary.sent == 6
    templates = sorted(t for _, t, _ in provider.sent)
    assert templates == sorted([
        "payment_reminder_upcoming", "payment_due_today", "payment_overdue",
        "payment_overdue", "payment_overdue_firm", "payment_final_notice",
    ])


def test_no_reminder_on_days_without_rule(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 5)
    summary, provider = run(db)
    assert summary.sent == 0 and provider.sent == []


def test_one_message_per_customer_with_total_and_most_severe_rule(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3, amount=1000)
    make_invoice(db, c, "INV2", 15, amount=2000, paid=500)
    make_invoice(db, c, "INV3", 1, amount=300)  # not triggering, but counted in the total
    summary, provider = run(db)
    assert summary.sent == 1
    to, template, params = provider.sent[0]
    assert template == "payment_overdue_firm"
    assert params[2] == "2,800.00"                 # 1000 + 1500 + 300
    assert params[3] == "INV2, INV1, INV3"         # oldest due first


def test_new_bill_trigger_uses_tone_of_oldest_overdue_bill(db):
    c = make_customer(db)
    make_invoice(db, c, "OLD", 31, amount=22000)  # 31 days: no rule fires on its own today
    make_invoice(db, c, "NEW", 0, amount=15750)   # due today: triggers a message
    summary, provider = run(db)
    _, template, params = provider.sent[0]
    assert template == "payment_final_notice"     # 30-day rule, not "due today"
    assert params[3] == "OLD, NEW"
    assert params[4] == (TODAY - timedelta(days=31)).strftime("%d-%b-%Y")


def test_skips_opted_out_paused_no_phone_and_disputed(db):
    make_invoice(db, make_customer(db, "A", whatsapp_opt_in=False), "A1", 3)
    make_invoice(db, make_customer(db, "B", reminders_paused=True), "B1", 3)
    make_invoice(db, make_customer(db, "C", phone=None), "C1", 3)
    make_invoice(db, make_customer(db, "D"), "D1", 3, status="disputed")
    summary, provider = run(db)
    assert provider.sent == []
    assert summary.skipped == 3  # disputed invoice is not even considered
    reasons = {l.error for l in db.scalars(select(ReminderLog)).all()}
    assert reasons == {"not opted in to WhatsApp", "reminders paused", "no phone number"}


def test_paid_invoices_ignored(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3, amount=1000, paid=1000)
    summary, provider = run(db)
    assert provider.sent == []


def test_skip_if_payment_recorded_today(db):
    c = make_customer(db)
    inv = make_invoice(db, c, "INV1", 3, amount=1000)
    db.add(Payment(invoice=inv, amount=Decimal(200), paid_on=TODAY))
    inv.amount_paid = Decimal(200)
    inv.refresh_status()
    db.commit()
    summary, provider = run(db)
    assert provider.sent == [] and summary.skipped == 1


def test_running_twice_same_day_does_not_resend(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3)
    run(db)
    summary, provider = run(db)
    assert provider.sent == [] and summary.already_done == 1


def test_outside_send_window(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3)
    summary, provider = run(db, now=datetime(2026, 11, 10, 22, 0))
    assert summary.outside_window and provider.sent == []


def test_failed_send_retried_once_and_logged(db):
    class Failing:
        name = "fail"
        calls = 0

        def send_template(self, to, template, params):
            Failing.calls += 1
            return SendResult(ok=False, error="131026: not a WhatsApp user", status="failed")

    c = make_customer(db)
    make_invoice(db, c, "INV1", 3)
    summary, _ = run(db, provider=Failing())
    assert summary.failed == 1 and Failing.calls == 2
    log = db.scalar(select(ReminderLog))
    assert log.status == "failed" and "131026" in log.error and log.attempts == 2


def test_call_list_orders_worst_first(db):
    a = make_customer(db, "A")
    b = make_customer(db, "B")
    make_invoice(db, a, "A1", 20, amount=500)
    make_invoice(db, b, "B1", 40, amount=100)
    make_invoice(db, b, "B2", 2, amount=100)
    rows = call_list(db, today=TODAY, min_days_overdue=15)
    assert [r["customer"].name for r in rows] == ["B", "A"]
    assert rows[0]["total"] == Decimal(200)
