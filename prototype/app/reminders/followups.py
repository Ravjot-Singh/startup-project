"""After a customer has paid everything: a thank-you now, and an invitation to order again later.

* Each 'fully_paid' event (app.payments) queues a thank-you straight away, and a reorder invite
  for when the customer would usually order again: their last bill date plus their usual gap
  between bills (median), or the owner's fallback number of days when there isn't enough history.
* The reorder invite is a marketing message: only for customers who agreed to offers and catalogues.
  It carries the current catalogue PDF, and is cancelled if the customer orders again first.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, settings, storage, ui
from app.models import (
    CANCELLED,
    KIND_REORDER,
    KIND_THANKS,
    OPENING,
    QUEUED,
    SKIPPED,
    Customer,
    CustomerEvent,
    Invoice,
    Message,
    Receipt,
)
from app.payments import FULLY_PAID
from app.whatsapp.provider import MediaFile, WhatsAppProvider
from app.whatsapp.templates import REORDER_TEMPLATE, THANKS_TEMPLATE, render

STALE_EVENT_DAYS = 3        # a payoff noticed later than this gets no thank-you (it would be odd so late)
GAP_LIMITS = (3, 60)        # a usual gap between orders outside this is ignored as unreliable
MIN_BILLS_FOR_RHYTHM = 3    # need at least this many bills (two gaps) to trust a customer's rhythm
CATALOGUE = "catalogue"


# ---------- when to invite a reorder ----------

def usual_gap_days(db: Session, customer_id: int) -> int | None:
    """Median days between a customer's recent bills, or None if there isn't enough history."""
    dates = sorted({d for (d,) in db.execute(
        select(Invoice.invoice_date).where(Invoice.customer_id == customer_id).order_by(Invoice.invoice_date.desc()).limit(12)
    ).all()})
    if len(dates) < MIN_BILLS_FOR_RHYTHM:
        return None
    gap = round(median((b - a).days for a, b in zip(dates, dates[1:])))
    return gap if GAP_LIMITS[0] <= gap <= GAP_LIMITS[1] else None


def reorder_date(db: Session, customer_id: int, paid_on: date) -> date:
    gap = usual_gap_days(db, customer_id) or settings.get_int(db, "reorder_fallback_days")
    last_bill = db.scalar(select(Invoice.invoice_date).where(Invoice.customer_id == customer_id).order_by(Invoice.invoice_date.desc()).limit(1))
    when = (last_bill or paid_on) + timedelta(days=gap)
    return max(when, paid_on + timedelta(days=1))  # never the same day as the thank-you


# ---------- turning payoffs into messages ----------

def process_fully_paid(db: Session, now: datetime) -> tuple[int, int]:
    """Queue a thank-you and a reorder invite for each new payoff. Returns (thanks, invites) queued."""
    events = db.scalars(select(CustomerEvent).where(CustomerEvent.kind == FULLY_PAID, CustomerEvent.handled_at.is_(None))
                        .order_by(CustomerEvent.id)).all()
    thanks = invites = 0
    for ev in events:
        ev.handled_at = now
        if (now.date() - ev.created_at.date()).days > STALE_EVENT_DAYS:
            continue
        customer = ev.customer
        if settings.get_bool(db, "send_thank_you"):
            db.add(Message(customer=customer, kind=KIND_THANKS, run_date=now.date(), scheduled_for=now, status=QUEUED,
                           amount_due=ev.amount or Decimal(0), idempotency_key=f"thanks:{ev.id}"))
            thanks += 1
        if settings.get_bool(db, "send_reorder_invites") and customer.marketing_opt_in:
            cancel_reorders(db, [customer.id], "Replaced by a newer invite")
            when = reorder_date(db, customer.id, ev.created_at.date())
            at = datetime.combine(when, time(settings.get_int(db, "reminder_run_hour")))
            db.add(Message(customer=customer, kind=KIND_REORDER, run_date=when, scheduled_for=at, status=QUEUED,
                           idempotency_key=f"reorder:{ev.id}"))
            invites += 1
    db.commit()
    return thanks, invites


def cancel_reorders(db: Session, customer_ids, reason: str = "Ordered again") -> int:
    pending = db.scalars(select(Message).where(Message.kind == KIND_REORDER, Message.status == QUEUED,
                                               Message.customer_id.in_(list(customer_ids)))).all()
    for m in pending:
        m.status, m.error = CANCELLED, reason
    return len(pending)


# ---------- the catalogue sent with invites ----------

def current_catalogue(db: Session):
    file_id = settings.get(db, "current_catalogue_file_id")
    return storage.load(db, int(file_id)) if file_id.isdigit() else None


def set_catalogue(db: Session, filename: str, data: bytes) -> None:
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise storage.StorageError("The catalogue must be a PDF file")
    old = current_catalogue(db)
    f = storage.save(db, CATALOGUE, filename, "application/pdf", data)
    settings.set_value(db, "current_catalogue_file_id", f.id)
    if old is not None:
        db.delete(old)
        db.commit()


def remove_catalogue(db: Session) -> None:
    old = current_catalogue(db)
    settings.set_value(db, "current_catalogue_file_id", "")
    if old is not None:
        db.delete(old)
        db.commit()


# ---------- sending ----------

def _last_payment(db: Session, customer_id: int) -> Decimal:
    amount = db.scalar(select(Receipt.amount).where(Receipt.customer_id == customer_id, Receipt.source != OPENING)
                       .order_by(Receipt.received_on.desc(), Receipt.id.desc()).limit(1))
    return Decimal(amount or 0)


def send(db: Session, provider: WhatsAppProvider, msg: Message, now: datetime) -> str:
    from app.reminders.engine import _record_result  # shared bookkeeping (attempts, retries)

    customer: Customer = msg.customer
    if msg.kind == KIND_THANKS:
        amount = msg.amount_due if msg.amount_due and msg.amount_due > 0 else _last_payment(db, customer.id)
        params = [customer.display_name, ui.inr(amount), config.BUSINESS_NAME]
        msg.template_name, msg.amount_due = THANKS_TEMPLATE, amount
        msg.message_preview = render(THANKS_TEMPLATE, params)
        return _record_result(db, msg, provider.send_template(customer.phone, THANKS_TEMPLATE, params), now)

    # reorder invite
    since = msg.created_at.date() if msg.created_at else now.date()
    if db.scalar(select(Invoice.id).where(Invoice.customer_id == customer.id, Invoice.invoice_date >= since).limit(1)):
        msg.status, msg.error = CANCELLED, "Ordered again"
        db.commit()
        return "cancelled"
    catalogue = current_catalogue(db)
    if catalogue is None:
        msg.status, msg.error = SKIPPED, "no catalogue uploaded (Settings)"
        db.commit()
        return "skipped"
    params = [customer.display_name, config.BUSINESS_NAME]
    msg.template_name = REORDER_TEMPLATE
    msg.message_preview = render(REORDER_TEMPLATE, params) + f"\n[Attached: {catalogue.filename}]"
    media = MediaFile(data=catalogue.data, mime=catalogue.content_type, filename=catalogue.filename)
    return _record_result(db, msg, provider.send_template(customer.phone, REORDER_TEMPLATE, params, media=media), now)
