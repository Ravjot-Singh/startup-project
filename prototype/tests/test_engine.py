from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import settings
from app.models import Customer, Invoice, Message, ReminderStep
from app.payments import record_payment
from app.reminders.engine import (
    MAX_ATTEMPTS,
    ScheduleError,
    call_list,
    cancel_message,
    plan_reminders,
    run_reminders,
    schedule_one_off,
    send_now,
    send_pending,
)
from app.whatsapp.provider import DryRunProvider, SendResult
from app.whatsapp.templates import render

TODAY = date(2026, 11, 10)
NOON = datetime(2026, 11, 10, 12, 0)


def make_customer(db, name="Sharma Store", phone="919800000001", **kw):
    c = Customer(name=name, shop_name=name, phone=phone, **kw)
    db.add(c)
    db.flush()
    return c


def make_invoice(db, customer, no, days_overdue, amount=1000, paid=0, status=None, today=TODAY):
    due = today - timedelta(days=days_overdue)
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
    return run_reminders(db, provider, today=now.date(), now=now), provider


def day(n, hour=12, minute=0):
    """n days after TODAY, at the given time."""
    return datetime.combine(TODAY + timedelta(days=n), datetime.min.time()).replace(hour=hour, minute=minute)


class Failing:
    """A provider that fails the first `fail_times` sends, then works."""

    name = "fail"

    def __init__(self, fail_times=99):
        self.fail_times, self.calls = fail_times, 0

    def send_template(self, to, template, params):
        self.calls += 1
        if self.calls <= self.fail_times:
            return SendResult(ok=False, error="131026: not a WhatsApp user", status="failed")
        return SendResult(ok=True, message_id=f"wamid.{self.calls}", status="sent")


# ---------- the schedule ----------

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


@pytest.mark.parametrize("days", [6, 10, 14, 18, 40])
def test_no_reminder_between_steps(db, days):
    make_invoice(db, make_customer(db), "INV1", days)
    summary, provider = run(db)
    assert summary.sent == 0 and provider.sent == []


@pytest.mark.parametrize("late_by", [1, 2])
def test_missed_step_still_goes_out_within_grace_days(db, late_by):
    make_invoice(db, make_customer(db), "INV1", 7 + late_by)  # the 7-day step was missed
    summary, provider = run(db)
    assert summary.sent == 1 and provider.sent[0][1] == "payment_overdue"


def test_each_step_goes_out_once_per_bill_even_over_days(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3)
    p = DryRunProvider()
    for n in range(4):  # days 3, 4, 5, 6 of the 3-day step's life
        run_reminders(db, p, now=day(n))
    assert len(p.sent) == 1
    assert db.query(ReminderStep).count() == 1


def test_one_message_per_customer_with_overdue_and_total(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 3, amount=1000)
    make_invoice(db, c, "INV2", 15, amount=2000, paid=500)
    make_invoice(db, c, "INV3", 1, amount=300)   # late, but no step today: still in the message
    make_invoice(db, c, "INV4", -5, amount=700)  # not due yet: only in the total
    summary, provider = run(db)
    assert summary.sent == 1
    _, template, params = provider.sent[0]
    assert template == "payment_overdue_firm"
    assert params[2] == "2,800"            # due or overdue now: 1000 + 1500 (after part payment) + 300
    assert params[3] == "3,500"            # total balance incl. the bill not due yet
    assert params[4] == "INV2, INV1, INV3" # oldest due first
    text = render(template, params)
    assert "Rs. 2,800" in text and "total balance Rs. 3,500" in text


def test_before_anything_is_due_the_message_is_about_the_next_bills(db):
    c = make_customer(db)
    make_invoice(db, c, "SOON", -3, amount=1200)
    make_invoice(db, c, "LATER", -20, amount=5000)
    _, provider = run(db)
    _, template, params = provider.sent[0]
    assert template == "payment_reminder_upcoming"
    assert (params[2], params[3], params[4]) == ("1,200", "6,200", "SOON")


def test_new_bill_trigger_uses_tone_of_oldest_overdue_bill(db):
    c = make_customer(db)
    make_invoice(db, c, "OLD", 40, amount=22000)  # past every step's grace: fires nothing itself
    make_invoice(db, c, "NEW", 0, amount=15750)   # due today: triggers a message
    _, provider = run(db)
    _, template, params = provider.sent[0]
    assert template == "payment_final_notice"     # 30-day tone, not "due today"
    assert params[4] == "OLD, NEW"
    assert params[5] == (TODAY - timedelta(days=40)).strftime("%d-%b-%Y")


def test_at_most_one_automatic_reminder_a_day(db):
    c = make_customer(db)
    make_invoice(db, c, "A", 3)
    p = DryRunProvider()
    run_reminders(db, p, now=NOON)
    make_invoice(db, c, "B", 0)  # a bill due today arrives after the morning's reminder
    s = run_reminders(db, p, now=NOON.replace(hour=15))
    assert len(p.sent) == 1 and s.already_done == 1
    s = run_reminders(db, p, now=day(1))  # next day, still within the due-date step's grace
    assert len(p.sent) == 2


def test_skips_opted_out_paused_no_phone_and_disputed(db):
    make_invoice(db, make_customer(db, "A", whatsapp_opt_in=False), "A1", 3)
    make_invoice(db, make_customer(db, "B", reminders_paused=True), "B1", 3)
    make_invoice(db, make_customer(db, "C", phone=None), "C1", 3)
    make_invoice(db, make_customer(db, "D"), "D1", 3, status="disputed")
    summary, provider = run(db)
    assert provider.sent == [] and summary.skipped == 3  # the disputed bill is not even considered
    assert {p.skip_reason for p in plan_reminders(db, TODAY)} == {"not opted in to WhatsApp", "reminders paused", "no phone number"}
    assert db.query(Message).count() == 0


def test_number_added_within_grace_days_still_gets_the_reminder(db):
    c = make_customer(db, phone=None)
    make_invoice(db, c, "INV1", 3)
    p = DryRunProvider()
    run_reminders(db, p, now=NOON)
    assert p.sent == []
    c.phone = "919800000009"
    db.commit()
    run_reminders(db, p, now=day(1))
    assert len(p.sent) == 1 and p.sent[0][0] == "919800000009"


def test_paid_invoices_ignored(db):
    make_invoice(db, make_customer(db), "INV1", 3, amount=1000, paid=1000)
    _, provider = run(db)
    assert provider.sent == []


def test_skip_if_payment_recorded_today(db):
    c = make_customer(db)
    inv = make_invoice(db, c, "INV1", 3, amount=1000)
    record_payment(db, c, Decimal(200), TODAY, allocations={inv.id: Decimal(200)})
    summary, provider = run(db)
    assert provider.sent == [] and summary.skipped == 1


def test_amount_after_part_payment(db):
    c = make_customer(db)
    inv = make_invoice(db, c, "INV1", 3, amount=10000)
    record_payment(db, c, Decimal(6500), TODAY - timedelta(days=1), allocations={inv.id: Decimal(6500)})
    _, provider = run(db)
    assert provider.sent[0][2][2] == "3,500" and provider.sent[0][2][3] == "3,500"


def test_running_twice_same_day_does_not_resend(db):
    make_invoice(db, make_customer(db), "INV1", 3)
    run(db)
    summary, provider = run(db)
    assert provider.sent == [] and db.query(Message).count() == 1


def test_outside_send_window(db):
    make_invoice(db, make_customer(db), "INV1", 3)
    summary, provider = run(db, now=datetime(2026, 11, 10, 22, 0))
    assert summary.outside_window and provider.sent == []


# ---------- retries ----------

def test_failed_send_is_retried_on_later_ticks_then_given_up(db):
    make_invoice(db, make_customer(db), "INV1", 3)
    p = Failing()
    s = run_reminders(db, p, now=NOON)
    msg = db.scalar(select(Message))
    assert s.failed == 1 and msg.status == "queued" and msg.attempts == 1 and "131026" in msg.error
    send_pending(db, p, NOON.replace(minute=10))
    send_pending(db, p, NOON.replace(minute=20))
    assert msg.status == "failed" and msg.attempts == MAX_ATTEMPTS
    send_pending(db, p, NOON.replace(minute=30))
    assert p.calls == MAX_ATTEMPTS  # no more tries


def test_retry_that_works_is_sent(db):
    make_invoice(db, make_customer(db), "INV1", 3)
    p = Failing(fail_times=1)
    run_reminders(db, p, now=NOON)
    send_pending(db, p, NOON.replace(minute=10))
    msg = db.scalar(select(Message))
    assert msg.status == "sent" and msg.attempts == 2 and msg.error is None


# ---------- the owner's own reminders ----------

def test_one_off_goes_out_at_its_time_once(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 40, amount=5000)
    schedule_one_off(db, c, day(1, 15), note="promised Friday", is_promise=True, now=NOON)
    p = DryRunProvider()
    send_pending(db, p, day(1, 14, 59))
    assert p.sent == []
    send_pending(db, p, day(1, 15))
    send_pending(db, p, day(1, 15, 10))
    assert len(p.sent) == 1 and p.sent[0][2][3] == "5,000"
    assert db.scalar(select(Message)).is_promise


def test_one_off_outside_sending_hours_waits_for_the_morning(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 40)
    schedule_one_off(db, c, day(0, 21, 30), now=NOON)
    p = DryRunProvider()
    send_pending(db, p, day(0, 21, 30))
    assert p.sent == []
    send_pending(db, p, day(1, 9, 0))
    assert len(p.sent) == 1


def test_one_off_cancelled_when_nothing_is_owed_by_then(db):
    c = make_customer(db)
    inv = make_invoice(db, c, "INV1", 40, amount=500)
    msg = schedule_one_off(db, c, day(1, 11), now=NOON)
    record_payment(db, c, Decimal(500), TODAY, allocations={inv.id: Decimal(500)})
    p = DryRunProvider()
    send_pending(db, p, day(1, 11))
    assert p.sent == [] and msg.status == "cancelled"


def test_one_off_needs_a_future_time_and_a_number(db):
    c = make_customer(db)
    with pytest.raises(ScheduleError, match="future"):
        schedule_one_off(db, c, NOON - timedelta(minutes=1), now=NOON)
    nophone = make_customer(db, "N", phone=None)
    with pytest.raises(ScheduleError, match="WhatsApp number"):
        schedule_one_off(db, nophone, day(1), now=NOON)


def test_cancel_a_one_off(db):
    c = make_customer(db)
    make_invoice(db, c, "INV1", 40)
    msg = schedule_one_off(db, c, day(1, 11), now=NOON)
    cancel_message(db, msg)
    p = DryRunProvider()
    send_pending(db, p, day(1, 11))
    assert p.sent == [] and msg.status == "cancelled"
    with pytest.raises(ScheduleError):
        cancel_message(db, msg)


def test_send_now_ignores_pause_and_sending_hours_but_not_opt_out(db):
    c = make_customer(db, reminders_paused=True)
    make_invoice(db, c, "INV1", 40, amount=900)
    settings.set_value(db, "sending_paused", True)
    p = DryRunProvider()
    msg = send_now(db, p, c, now=datetime(2026, 11, 10, 22, 30))
    assert msg.is_sent and msg.kind == "manual" and len(p.sent) == 1
    c.whatsapp_opt_in = False
    db.commit()
    msg = send_now(db, p, c, now=NOON)
    assert msg.status == "skipped" and len(p.sent) == 1


# ---------- call list ----------

def test_call_list_orders_worst_first(db):
    a = make_customer(db, "A")
    b = make_customer(db, "B")
    make_invoice(db, a, "A1", 20, amount=500)
    make_invoice(db, b, "B1", 40, amount=100)
    make_invoice(db, b, "B2", 2, amount=100)
    rows = call_list(db, today=TODAY, min_days_overdue=15)
    assert [r["customer"].name for r in rows] == ["B", "A"]
    assert rows[0]["total"] == Decimal(200)
