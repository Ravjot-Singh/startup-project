"""Payment reminders: who gets what, and the outbox that sends it.

Automatic reminders (the schedule)
  * Each schedule step (ReminderRule) fires once per bill when the bill reaches that many days from
    its due date. If the app missed that day (server asleep, report uploaded late), the step still
    fires within GRACE_DAYS. ReminderStep remembers which (bill, step) pairs are done.
  * One message per customer covers all their unpaid bills. Its tone follows the OLDEST unpaid bill,
    and a customer gets at most one automatic reminder a day.

Every message (automatic, one-off "remind me on ...", or "send now") is a row in the outbox (Message).
It is written when it is sent, so it always quotes the latest balance; if nothing is owed any more by
then, it is cancelled. A failed send is retried on the next ticks, up to MAX_ATTEMPTS.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app import bills, clock, config, settings, ui
from app.models import (
    CANCELLED,
    FAILED,
    KIND_BILL,
    KIND_MANUAL,
    KIND_ONE_OFF,
    KIND_REMINDER,
    OPENING,
    PAID,
    QUEUED,
    SKIPPED,
    UNPAID_STATUSES,
    Customer,
    Invoice,
    Message,
    Receipt,
    ReminderRule,
    ReminderStep,
)
from app.whatsapp.provider import MediaFile, WhatsAppProvider
from app.whatsapp.templates import BILL_TEMPLATE, render

GRACE_DAYS = 3  # a missed step still goes out up to this many days late
MAX_ATTEMPTS = 3
MAX_INVOICES_IN_MESSAGE = 3
AUTO_KINDS = (KIND_REMINDER, KIND_ONE_OFF, KIND_BILL)  # held back by "Pause all sending" and the sending hours
BILL_SEND_DELAY = timedelta(minutes=30)  # time to upload Busy's PDFs after the outstanding report
BILL_MAX_AGE_DAYS = 3  # only bills this recent are sent automatically (not years of history on the first upload)

# Tone when no schedule step applies (no active steps at all)
FALLBACK_TEMPLATES = ((1, "payment_overdue"), (0, "payment_due_today"), (-10**6, "payment_reminder_upcoming"))


@dataclass
class RunSummary:
    run_date: date
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    already_done: int = 0
    outside_window: bool = False
    details: list[str] = field(default_factory=list)


# ---------- message content ----------

def fmt_amount(value: Decimal) -> str:
    return ui.inr(value)


def upi_link(amount: Decimal, note: str) -> str:
    if not config.BUSINESS_UPI_ID:
        return "contact us for payment details"
    return (
        f"upi://pay?pa={quote(config.BUSINESS_UPI_ID)}&pn={quote(config.BUSINESS_NAME)}"
        f"&am={amount:.2f}&cu=INR&tn={quote(note)}"
    )


@dataclass
class Content:
    invoices: list[Invoice]  # every unpaid bill, oldest due first
    focus: list[Invoice]     # the bills the message is about: due or overdue (else the next ones coming due)
    focus_amount: Decimal
    total: Decimal
    params: list[str]


def message_content(customer: Customer, invoices: list[Invoice], today: date) -> Content | None:
    """Template parameters ({{1}}..{{7}}, see app/whatsapp/templates.py), or None if nothing is owed."""
    unpaid = sorted((i for i in invoices if i.status in UNPAID_STATUSES and i.balance > 0), key=lambda i: (i.due_date, i.id))
    if not unpaid:
        return None
    focus = [i for i in unpaid if i.days_overdue(today) >= 0]
    if not focus:
        first_due = unpaid[0].due_date
        focus = [i for i in unpaid if i.due_date == first_due]
    focus_amount = sum((i.balance for i in focus), Decimal(0))
    total = sum((i.balance for i in unpaid), Decimal(0))
    nos = ", ".join(i.invoice_no for i in focus[:MAX_INVOICES_IN_MESSAGE])
    if len(focus) > MAX_INVOICES_IN_MESSAGE:
        nos += f" +{len(focus) - MAX_INVOICES_IN_MESSAGE} more"
    params = [
        customer.display_name,
        config.BUSINESS_NAME,
        fmt_amount(focus_amount),
        fmt_amount(total),
        nos,
        focus[0].due_date.strftime("%d-%b-%Y"),
        upi_link(focus_amount, f"Dues {customer.code or customer.id}"),
    ]
    return Content(invoices=unpaid, focus=focus, focus_amount=focus_amount, total=total, params=params)


def active_rules(db: Session) -> list[ReminderRule]:
    return list(db.scalars(select(ReminderRule).where(ReminderRule.is_active).order_by(ReminderRule.offset_days)).all())


def tone_template(rules: list[ReminderRule], invoices: list[Invoice], today: date) -> tuple[ReminderRule | None, str]:
    """The step whose tone fits the customer's OLDEST unpaid bill, and its template."""
    worst = max(i.days_overdue(today) for i in invoices)
    reached = [r for r in rules if r.offset_days <= worst]
    if reached:
        r = reached[-1]
        return r, r.template_name
    if rules:  # nothing reached yet (bills not due): the gentlest step
        return rules[0], rules[0].template_name
    return None, next(t for limit, t in FALLBACK_TEMPLATES if worst >= limit)


def skip_reason(db: Session, customer: Customer, today: date, kind: str = KIND_REMINDER) -> str | None:
    if not customer.is_active:
        return "customer inactive"
    if not customer.whatsapp_opt_in:
        return "not opted in to WhatsApp"
    if not customer.phone:
        return "no phone number"
    if customer.reminders_paused and kind not in (KIND_MANUAL, KIND_BILL):
        return "reminders paused"
    if kind == KIND_REMINDER:
        paid_today = db.scalar(
            select(Receipt.id).where(Receipt.customer_id == customer.id, Receipt.received_on == today, Receipt.source != OPENING).limit(1)
        )
        if paid_today:
            return "payment recorded today"
    return None


def unpaid_by_customer(db: Session, customer_ids: list[int] | None = None) -> dict[int, list[Invoice]]:
    stmt = select(Invoice).options(joinedload(Invoice.customer)).where(Invoice.status.in_(UNPAID_STATUSES))
    if customer_ids is not None:
        stmt = stmt.where(Invoice.customer_id.in_(customer_ids))
    out: dict[int, list[Invoice]] = defaultdict(list)
    for inv in db.scalars(stmt).all():
        if inv.balance > 0:
            out[inv.customer_id].append(inv)
    return out


# ---------- the schedule ----------

def auto_key(customer_id: int, day: date) -> str:
    return f"auto:{customer_id}:{day.isoformat()}"


@dataclass
class PlannedReminder:
    """A customer with schedule steps due today, and what the reminder would say."""

    customer: Customer
    rule: ReminderRule  # the tone
    steps: list[tuple[Invoice, ReminderRule]]
    content: Content
    skip_reason: str | None = None
    existing: Message | None = None  # today's automatic reminder, if one was already made

    @property
    def total(self) -> Decimal:
        return self.content.total

    @property
    def params(self) -> list[str]:
        return self.content.params


def plan_reminders(db: Session, today: date) -> list[PlannedReminder]:
    """Who has schedule steps due today. Read-only: used by the run itself and the Today page."""
    rules = active_rules(db)
    if not rules:
        return []
    by_customer = unpaid_by_customer(db)
    bill_ids = [i.id for invs in by_customer.values() for i in invs]
    done = set(db.execute(select(ReminderStep.invoice_id, ReminderStep.rule_id).where(ReminderStep.invoice_id.in_(bill_ids))).all()) if bill_ids else set()

    plan = []
    for cid in sorted(by_customer):
        invoices = by_customer[cid]
        steps = [
            (inv, r) for inv in invoices for r in rules
            if 0 <= inv.days_overdue(today) - r.offset_days < GRACE_DAYS and (inv.id, r.id) not in done
        ]
        if not steps:
            continue
        customer = invoices[0].customer
        rule, _ = tone_template(rules, invoices, today)
        plan.append(PlannedReminder(
            customer=customer, rule=rule, steps=steps, content=message_content(customer, invoices, today),
            skip_reason=skip_reason(db, customer, today),
            existing=db.scalar(select(Message).where(Message.idempotency_key == auto_key(cid, today))),
        ))
    return plan


def enqueue_due_reminders(db: Session, now: datetime, summary: RunSummary | None = None) -> list[Message]:
    """Put today's due schedule steps in the outbox (one message per customer) and mark the steps done."""
    today = now.date()
    created = []
    for p in plan_reminders(db, today):
        if p.existing:
            if summary:
                summary.already_done += 1
            continue
        if p.skip_reason:  # not queued: if fixed within the grace days (e.g. number added), it still goes out
            if summary:
                summary.skipped += 1
                summary.details.append(f"Skipped {p.customer.display_name}: {p.skip_reason}")
            continue
        msg = Message(customer=p.customer, kind=KIND_REMINDER, rule=p.rule, template_name=p.rule.template_name,
                      run_date=today, scheduled_for=now, status=QUEUED, idempotency_key=auto_key(p.customer.id, today))
        db.add(msg)
        db.flush()
        for inv, rule in p.steps:
            db.add(ReminderStep(invoice_id=inv.id, rule_id=rule.id, message_id=msg.id))
        created.append(msg)
    db.commit()
    return created


# ---------- the outbox ----------

def send_one(db: Session, provider: WhatsAppProvider, msg: Message, now: datetime) -> str:
    """Write and send one message. Returns 'sent', 'retrying', 'failed', 'skipped' or 'cancelled'."""
    today = now.date()
    customer = msg.customer
    reason = skip_reason(db, customer, today, msg.kind)
    if reason:
        msg.status, msg.error = SKIPPED, reason
        db.commit()
        return "skipped"
    if msg.kind == KIND_BILL:
        return _send_bill(db, provider, msg, now)
    invoices = unpaid_by_customer(db, [customer.id]).get(customer.id, [])
    content = message_content(customer, invoices, today)
    if content is None:
        msg.status, msg.error = CANCELLED, "Nothing owed any more"
        db.commit()
        return "cancelled"
    if not msg.template_name or msg.kind != KIND_REMINDER:
        rule, template = tone_template(active_rules(db), content.invoices, today)
        msg.template_name = template
        msg.rule = msg.rule or rule
    msg.invoice_nos = ",".join(i.invoice_no for i in content.invoices)
    msg.amount_due = content.total
    msg.message_preview = render(msg.template_name, content.params)

    result = provider.send_template(customer.phone, msg.template_name, content.params)
    return _record_result(db, msg, result, now)


def _record_result(db: Session, msg: Message, result, now: datetime) -> str:
    msg.attempts = (msg.attempts or 0) + 1
    if result.ok:
        msg.status, msg.provider_msg_id, msg.error, msg.sent_at = result.status, result.message_id, None, now
        outcome = "sent"
    else:
        msg.error = result.error
        msg.status = FAILED if msg.attempts >= MAX_ATTEMPTS else QUEUED  # queued again = retried next tick
        outcome = "failed" if msg.status == FAILED else "retrying"
    db.commit()
    return outcome


def bill_params(inv: Invoice) -> list[str]:
    balance = max(inv.balance, Decimal(0))
    return [
        inv.customer.display_name,
        config.BUSINESS_NAME,
        inv.invoice_no,
        inv.invoice_date.strftime("%d-%b-%Y"),
        fmt_amount(inv.amount),
        fmt_amount(balance),
        inv.due_date.strftime("%d-%b-%Y"),
        upi_link(balance, f"Bill {inv.invoice_no}") if balance > 0 else "nothing due, thank you",
    ]


def _send_bill(db: Session, provider: WhatsAppProvider, msg: Message, now: datetime) -> str:
    inv = msg.invoice
    if inv is None:
        msg.status, msg.error = CANCELLED, "Bill no longer exists"
        db.commit()
        return "cancelled"
    if msg.idempotency_key and inv.status == PAID:  # automatic bill message, but already paid: not needed
        msg.status, msg.error = CANCELLED, "Already paid"
        db.commit()
        return "cancelled"
    pdf = bills.bill_pdf(db, inv)
    params = bill_params(inv)
    msg.template_name = BILL_TEMPLATE
    msg.invoice_nos = inv.invoice_no
    msg.amount_due = max(inv.balance, Decimal(0))
    msg.message_preview = render(BILL_TEMPLATE, params) + f"\n[Attached: {pdf.filename}" + (" from Busy]" if pdf.from_busy else ", bill summary]")
    result = provider.send_template(msg.customer.phone, BILL_TEMPLATE, params,
                                    media=MediaFile(data=pdf.data, mime=bills.PDF_MIME, filename=pdf.filename))
    return _record_result(db, msg, result, now)


def queue_new_bills(db: Session, invoices: list[Invoice], now: datetime | None = None) -> list[Message]:
    """After an upload from Busy: queue each new, recent bill's PDF to its customer (once per bill)."""
    now = now or clock.now()
    if not settings.get_bool(db, "send_bills_automatically"):
        return []
    queued = []
    for inv in invoices:
        if (now.date() - inv.invoice_date).days > BILL_MAX_AGE_DAYS or inv.status == PAID:
            continue
        key = f"bill:{inv.id}"
        if db.scalar(select(Message.id).where(Message.idempotency_key == key)):
            continue
        msg = Message(customer_id=inv.customer_id, kind=KIND_BILL, invoice_id=inv.id, run_date=now.date(),
                      scheduled_for=now + BILL_SEND_DELAY, status=QUEUED, idempotency_key=key)
        db.add(msg)
        queued.append(msg)
    db.commit()
    return queued


def send_bill_now(db: Session, provider: WhatsAppProvider, inv: Invoice, now: datetime | None = None) -> Message:
    """The owner pressed "Send bill": goes out straight away, with the Busy PDF if uploaded."""
    now = now or clock.now()
    msg = Message(customer_id=inv.customer_id, kind=KIND_BILL, invoice_id=inv.id, run_date=now.date(),
                  scheduled_for=now, status=QUEUED)
    db.add(msg)
    db.flush()
    send_one(db, provider, msg, now)
    return msg


def due_messages(db: Session, now: datetime, kinds=AUTO_KINDS) -> list[Message]:
    return list(db.scalars(
        select(Message).options(joinedload(Message.customer))
        .where(Message.status == QUEUED, Message.kind.in_(kinds),
               (Message.scheduled_for.is_(None)) | (Message.scheduled_for <= now))
        .order_by(Message.scheduled_for, Message.id)
    ).all())


def in_send_window(db: Session, now: datetime) -> tuple[bool, int, int]:
    start, end = settings.get_int(db, "send_window_start"), settings.get_int(db, "send_window_end")
    return start <= now.hour < end, start, end


def send_due(db: Session, provider: WhatsAppProvider, now: datetime, summary: RunSummary) -> RunSummary:
    """Send everything in the outbox whose time has come (automatic and one-off reminders, retries)."""
    for msg in due_messages(db, now):
        outcome = send_one(db, provider, msg, now)
        name = msg.customer.display_name
        if outcome == "sent":
            summary.sent += 1
            summary.details.append(f"{ui.tone(msg.template_name)} -> {name} (Rs. {fmt_amount(msg.amount_due)})")
        elif outcome in ("failed", "retrying"):
            summary.failed += 1
            summary.details.append(f"FAILED {name}: {msg.error}" + (" (will retry)" if outcome == "retrying" else ""))
        elif outcome == "skipped":
            summary.skipped += 1
            summary.details.append(f"Skipped {name}: {msg.error}")
        else:
            summary.cancelled += 1
    return summary


def run_reminders(
    db: Session,
    provider: WhatsAppProvider,
    today: date | None = None,
    now: datetime | None = None,
    enforce_window: bool = True,
) -> RunSummary:
    """Queue today's due schedule steps and send everything that is due."""
    now = now or (datetime.combine(today, time(12, 0)) if today else clock.now())
    today = today or now.date()
    if now.date() != today:
        now = datetime.combine(today, now.time())
    summary = RunSummary(run_date=today)
    ok, start, end = in_send_window(db, now)
    if enforce_window and not ok:
        summary.outside_window = True
        summary.details.append(f"Not sending: {now:%H:%M} is outside the {start}:00-{end}:00 window")
        return summary
    if not active_rules(db):
        summary.details.append("No active reminder steps")
    enqueue_due_reminders(db, now, summary)
    return send_due(db, provider, now, summary)


def send_pending(db: Session, provider: WhatsAppProvider, now: datetime) -> RunSummary:
    """Before the daily run hour: only one-off reminders and retries whose time has come."""
    summary = RunSummary(run_date=now.date())
    ok, _, _ = in_send_window(db, now)
    if not ok:
        summary.outside_window = True
        return summary
    return send_due(db, provider, now, summary)


# ---------- the owner's own reminders ----------

class ScheduleError(ValueError):
    """Shown to the owner as-is."""


def send_now(db: Session, provider: WhatsAppProvider, customer: Customer, now: datetime | None = None) -> Message:
    """The owner pressed "Send reminder now": goes out straight away, even outside sending hours or when paused."""
    now = now or clock.now()
    msg = Message(customer=customer, kind=KIND_MANUAL, run_date=now.date(), scheduled_for=now, status=QUEUED)
    db.add(msg)
    db.flush()
    send_one(db, provider, msg, now)
    return msg


def schedule_one_off(db: Session, customer: Customer, when: datetime, note: str | None = None,
                     is_promise: bool = False, now: datetime | None = None) -> Message:
    """'Remind on <date> at <time>' for one customer, e.g. the day they promised to pay."""
    now = now or clock.now()
    if when <= now:
        raise ScheduleError("Choose a time in the future")
    if not customer.phone:
        raise ScheduleError("Add a WhatsApp number for this customer first")
    msg = Message(customer=customer, kind=KIND_ONE_OFF, run_date=when.date(), scheduled_for=when, status=QUEUED,
                  note=(note or "").strip()[:200] or None, is_promise=is_promise)
    db.add(msg)
    db.commit()
    return msg


def cancel_message(db: Session, msg: Message) -> None:
    if msg.status != QUEUED:
        raise ScheduleError("That reminder has already been handled")
    msg.status, msg.error = CANCELLED, "Cancelled by you"
    db.commit()


def call_list(db: Session, today: date | None = None, min_days_overdue: int = 15) -> list[dict]:
    """Customers whose oldest unpaid invoice is at least min_days_overdue late, worst first."""
    today = today or clock.today()
    rows: dict[int, dict] = {}
    for cid, invoices in unpaid_by_customer(db).items():
        r = rows.setdefault(cid, {"customer": invoices[0].customer, "total": Decimal("0"), "max_overdue": -10**6, "count": 0})
        for inv in invoices:
            r["total"] += inv.balance
            r["count"] += 1
            r["max_overdue"] = max(r["max_overdue"], inv.days_overdue(today))
    result = [r for r in rows.values() if r["max_overdue"] >= min_days_overdue]
    return sorted(result, key=lambda r: (-r["max_overdue"], -r["total"]))
