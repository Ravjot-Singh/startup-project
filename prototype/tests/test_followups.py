"""After full payment: thank-you now, reorder invite later (with the catalogue, only with consent)."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import settings
from app.importers.outstanding import import_outstanding
from app.jobs import run_tick
from app.models import Customer, CustomerEvent, Invoice, Message
from app.payments import record_payment
from app.reminders import followups
from app.reminders.followups import reorder_date, usual_gap_days
from app.routes.webhooks import handle_payload
from app.whatsapp.provider import DryRunProvider

TODAY = date(2026, 11, 10)
NOON = datetime(2026, 11, 10, 12, 0)
CATALOGUE = b"%PDF-1.4 catalogue"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Timestamps the app records itself (payoff time, when a message was queued) use the test's 'today'."""
    from app import clock

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 10, 12, 0, tzinfo=tz)

    monkeypatch.setattr(clock, "datetime", Frozen)


def cust(db, name="Sharma Store", marketing=True, **kw):
    c = Customer(name=name, shop_name=name, phone=kw.pop("phone", "919800000001"), marketing_opt_in=marketing, **kw)
    db.add(c)
    db.flush()
    return c


def bill(db, c, no, invoice_date, amount=1000, paid=0):
    inv = Invoice(customer=c, invoice_no=no, invoice_date=invoice_date, due_date=invoice_date + timedelta(days=15),
                  amount=Decimal(amount), amount_paid=Decimal(paid))
    inv.refresh_status()
    db.add(inv)
    db.commit()
    return inv


def pay_off(db, c, inv, on=TODAY):
    record_payment(db, c, inv.balance, on, allocations={inv.id: inv.balance})


def messages(db, kind):
    return list(db.scalars(select(Message).where(Message.kind == kind).order_by(Message.id)).all())


def tick(db, now, p=None):
    p = p or DryRunProvider()
    run_tick(db, p, now=now)
    return p


# ---------- when to invite ----------

def test_usual_gap_is_the_median_of_recent_bill_gaps(db):
    c = cust(db)
    for i, d in enumerate([0, 10, 19, 31, 40]):  # gaps 10, 9, 12, 9 -> median 9.5 -> 10 (rounded)
        bill(db, c, f"B{i}", date(2026, 9, 1) + timedelta(days=d), paid=1000)
    assert usual_gap_days(db, c.id) in (9, 10)


def test_too_few_bills_or_odd_gaps_mean_no_rhythm(db):
    c = cust(db)
    bill(db, c, "A", date(2026, 10, 1), paid=1000)
    bill(db, c, "B", date(2026, 10, 11), paid=1000)
    assert usual_gap_days(db, c.id) is None  # only one gap
    d = cust(db, "Rare", phone="919800000002")
    for i, day in enumerate([date(2026, 1, 1), date(2026, 5, 1), date(2026, 9, 1)]):
        bill(db, d, f"R{i}", day, paid=1000)
    assert usual_gap_days(db, d.id) is None  # every 4 months: too long to be a rhythm


def test_reorder_date_uses_rhythm_or_fallback(db):
    c = cust(db)
    for i, d in enumerate([date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 29)]):
        bill(db, c, f"B{i}", d, paid=1000)
    assert reorder_date(db, c.id, TODAY) == date(2026, 11, 12)  # last bill 29 Oct + 14 days
    new = cust(db, "New", phone="919800000003")
    bill(db, new, "N1", date(2026, 11, 1), paid=1000)
    settings.set_value(db, "reorder_fallback_days", 5)
    assert reorder_date(db, new.id, TODAY) == date(2026, 11, 11)  # 1 Nov + 5 = 6 Nov is past: next day after payoff


# ---------- thank-you and invite ----------

def test_payoff_sends_thanks_now_and_invite_later_with_catalogue(db):
    followups.set_catalogue(db, "Catalogue Nov.pdf", CATALOGUE)
    c = cust(db)
    for i, d in enumerate([date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27)]):
        bill(db, c, f"OLD{i}", d, paid=1000)
    inv = bill(db, c, "B1", date(2026, 11, 3), amount=4500)
    pay_off(db, c, inv)
    p = tick(db, NOON)
    assert [(t, params) for _, t, params in p.sent] == [("payment_thanks", ["Sharma Store", "4,500", p.sent[0][2][2]])]
    (invite,) = messages(db, "reorder")
    assert invite.scheduled_for == datetime(2026, 11, 11, 10, 0)  # 3 Nov + 7 = 10 Nov is the payoff day itself: the day after, at the send time
    p = tick(db, datetime(2026, 11, 11, 10, 5))
    assert [t for _, t, _ in p.sent] == ["reorder_invite"]
    assert p.media[0][2].data == CATALOGUE and p.media[0][2].filename == "Catalogue Nov.pdf"
    assert db.get(Message, invite.id).is_sent


def test_thanks_only_once_per_payoff(db):
    c = cust(db)
    inv = bill(db, c, "B1", date(2026, 11, 1))
    pay_off(db, c, inv)
    p = DryRunProvider()
    for minute in (0, 10, 20):
        tick(db, NOON + timedelta(minutes=minute), p)
    assert [t for _, t, _ in p.sent].count("payment_thanks") == 1
    inv2 = bill(db, c, "B2", date(2026, 11, 9))  # a new order, later paid off too
    pay_off(db, c, inv2)
    tick(db, NOON + timedelta(minutes=30), p)
    assert [t for _, t, _ in p.sent].count("payment_thanks") == 2


def test_no_invite_without_consent_but_still_thanked(db):
    c = cust(db, marketing=False)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    p = tick(db, NOON)
    assert [t for _, t, _ in p.sent] == ["payment_thanks"] and messages(db, "reorder") == []


def test_invite_cancelled_when_they_order_again_first(db):
    followups.set_catalogue(db, "c.pdf", CATALOGUE)
    c = cust(db)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    tick(db, NOON)
    (invite,) = messages(db, "reorder")
    import_outstanding(db, "busy.csv", b"Bill No,Party Name,Bill Date,Bill Amount,Pending Amount\nB2,Sharma Store,10-11-2026,800,800\n",
                       full_outstanding_report=False, today=TODAY)
    assert invite.status == "cancelled" and invite.error == "Ordered again"


def test_invite_cancelled_at_send_time_if_a_bill_appeared_another_way(db):
    followups.set_catalogue(db, "c.pdf", CATALOGUE)
    c = cust(db)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    tick(db, NOON)
    (invite,) = messages(db, "reorder")
    bill(db, c, "B2", TODAY, amount=600)
    p = tick(db, invite.scheduled_for)
    assert "reorder_invite" not in [t for _, t, _ in p.sent] and invite.status == "cancelled"


def test_invite_without_a_catalogue_is_not_sent_and_needs_you(db):
    from app.dashboard import build_today

    c = cust(db)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    tick(db, NOON)
    assert build_today(db, NOON).invites_without_catalogue == 1
    (invite,) = messages(db, "reorder")
    p = tick(db, invite.scheduled_for)
    assert p.media == [] and invite.status == "skipped" and "no catalogue" in invite.error


def test_invite_skipped_if_consent_withdrawn_or_paused(db):
    followups.set_catalogue(db, "c.pdf", CATALOGUE)
    c = cust(db)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    tick(db, NOON)
    (invite,) = messages(db, "reorder")
    c.marketing_opt_in = False
    db.commit()
    tick(db, invite.scheduled_for)
    assert invite.status == "skipped" and invite.error == "hasn't agreed to offers and catalogues"


def test_switching_follow_ups_off(db):
    settings.set_value(db, "send_thank_you", False)
    settings.set_value(db, "send_reorder_invites", False)
    c = cust(db)
    pay_off(db, c, bill(db, c, "B1", date(2026, 11, 1)))
    p = tick(db, NOON)
    assert p.sent == [] and messages(db, "thanks") == messages(db, "reorder") == []


def test_late_noticed_payoff_gets_no_thanks(db):
    c = cust(db)
    db.add(CustomerEvent(customer=c, kind="fully_paid", amount=Decimal(100), created_at=NOON - timedelta(days=5)))
    db.commit()
    p = tick(db, NOON)
    assert p.sent == [] and db.scalar(select(CustomerEvent)).handled_at is not None


def test_thanks_after_busy_upload_quotes_what_busy_showed_paid(db):
    head = "Bill No,Party Name,Bill Date,Bill Amount,Pending Amount\n"
    import_outstanding(db, "d1.csv", (head + "B1,Gupta Kirana,01-11-2026,5000,5000\n").encode(), today=TODAY)
    import_outstanding(db, "d2.csv", (head + "X1,Someone Else,05-11-2026,10,10\n").encode(), full_outstanding_report=True, today=TODAY)
    c = db.scalar(select(Customer).where(Customer.name == "Gupta Kirana"))
    c.phone = "919800000009"
    db.commit()
    p = tick(db, NOON)
    thanks = [params for _, t, params in p.sent if t == "payment_thanks"]
    assert thanks == [["Gupta Kirana", "5,000", thanks[0][2]]]


@pytest.mark.parametrize("text,whatsapp,marketing", [
    ("Stop promotions", True, False), ("STOP", False, False), ("Thanks", True, True),
])
def test_stop_words(db, text, whatsapp, marketing):
    c = cust(db)
    db.commit()
    handle_payload(db, {"entry": [{"changes": [{"value": {"messages": [
        {"from": c.phone, "type": "button", "button": {"text": text}}]}}]}]})
    assert (c.whatsapp_opt_in, c.marketing_opt_in) == (whatsapp, marketing)


def test_catalogue_must_be_a_pdf_and_replacing_removes_the_old(db):
    from app import storage
    from app.models import StoredFile

    with pytest.raises(storage.StorageError):
        followups.set_catalogue(db, "photo.jpg", b"\xff\xd8")
    followups.set_catalogue(db, "a.pdf", CATALOGUE)
    followups.set_catalogue(db, "b.pdf", CATALOGUE + b"2")
    assert db.query(StoredFile).count() == 1 and followups.current_catalogue(db).filename == "b.pdf"
    followups.remove_catalogue(db)
    assert followups.current_catalogue(db) is None and db.query(StoredFile).count() == 0
