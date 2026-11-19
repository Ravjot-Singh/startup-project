"""Everything the Today page shows, most important first:

1. Three numbers: collected in the last 7 days, due to you, overdue.
2. Needs you: replies to read, messages that failed, missed payment promises, customers to call,
   customers we can't message.
3. Going out today: what has gone out, what is waiting (and until when), and who is left out and why.
4. Details (folded away): ageing and biggest dues.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app import config, settings, ui
from app.models import (
    CANCELLED,
    FAILED,
    KIND_MANUAL,
    KIND_ONE_OFF,
    OPENING,
    QUEUED,
    SKIPPED,
    UNPAID_STATUSES,
    Customer,
    ImportBatch,
    InboundMessage,
    Invoice,
    Message,
    Receipt,
)
from app.payments import outstanding
from app.reminders.engine import MAX_ATTEMPTS, call_list, plan_reminders

CALL_AFTER_DAYS = 15
PROMISE_FOLLOW_UP_DAYS = 14  # a missed promise stays under "Needs you" this long


@dataclass
class OutgoingItem:
    customer: Customer
    label: str       # tone or kind of message
    amount: Decimal
    state: str       # "sent" | "failed" | "skipped" | "waiting" | "paused" | "cancelled"
    note: str


@dataclass
class MissedPromise:
    message: Message
    owed: Decimal


@dataclass
class SendingStatus:
    paused: bool
    test_mode: bool
    run_hour: int
    window_start: int
    window_end: int
    test_group: int | None = None  # number of test numbers when "test group only" is on, else None

    @property
    def live_for_test_group(self) -> bool:
        return not self.test_mode and self.test_group is not None

    @property
    def summary(self) -> str:
        if self.paused:
            return "Automatic sending is paused"
        return f"Sends automatically every day from {self.run_hour}:00 (only between {self.window_start}:00 and {self.window_end}:00)"


@dataclass
class TodayView:
    today: date
    collected_7d: Decimal
    due_total: Decimal
    overdue_total: Decimal
    customers_due: int
    customers_overdue: int
    replies: list[InboundMessage]
    replies_count: int
    failed: list[Message]
    promises: list[MissedPromise]
    to_call: list[dict]
    to_call_count: int
    invites_without_catalogue: int
    no_phone_count: int
    outgoing: list[OutgoingItem]
    status: SendingStatus
    last_import: ImportBatch | None
    buckets: dict[str, Decimal] = field(default_factory=dict)
    top: list[dict] = field(default_factory=list)

    @property
    def needs_count(self) -> int:
        """Number of things to look at, matching the rows shown (each group of customers is one row)."""
        return (self.replies_count + len(self.failed) + len(self.promises) + (1 if self.invites_without_catalogue else 0)
                + (1 if self.to_call_count else 0) + (1 if self.no_phone_count else 0))

    @property
    def has_waiting(self) -> bool:
        return any(o.state == "waiting" for o in self.outgoing)

    @property
    def has_data(self) -> bool:
        return self.last_import is not None or self.due_total > 0

    @property
    def import_age_days(self) -> int | None:
        return (self.today - self.last_import.created_at.date()).days if self.last_import else None


def sending_status(db: Session) -> SendingStatus:
    return SendingStatus(
        paused=settings.get_bool(db, "sending_paused"),
        test_mode=config.WHATSAPP_PROVIDER != "meta",
        run_hour=settings.get_int(db, "reminder_run_hour"),
        window_start=settings.get_int(db, "send_window_start"),
        window_end=settings.get_int(db, "send_window_end"),
        test_group=len(settings.test_group_phones(db)) if settings.get_bool(db, "test_group_only") else None,
    )


def _label(msg: Message) -> str:
    return ui.message_label(msg)


def _waiting_note(status: SendingStatus, now: datetime, at: datetime | None) -> tuple[str, str]:
    if status.paused:
        return "paused", "Paused"
    if at and at > now:
        return "waiting", f"Sends at {at:%H:%M}"
    if not (status.window_start <= now.hour < status.window_end):
        return "waiting", f"Outside sending hours, next chance {status.window_start}:00"
    return "waiting", "Sending in the next few minutes"


def _outgoing(db: Session, status: SendingStatus, now: datetime, balances: dict[int, Decimal]) -> list[OutgoingItem]:
    today = now.date()
    items = []
    todays = db.scalars(
        select(Message).options(joinedload(Message.customer))
        .where(or_(Message.run_date == today,
                   Message.scheduled_for.between(datetime.combine(today, time.min), datetime.combine(today, time.max))))
    ).all()
    seen_ids = set()
    for m in todays:
        seen_ids.add(m.id)
        if m.is_sent or m.status == FAILED:
            amount = m.amount_due
        elif m.kind == "bill" and m.invoice is not None:
            amount = max(m.invoice.balance, Decimal(0))
        else:
            amount = balances.get(m.customer_id, Decimal(0))
        if m.is_sent:
            items.append(OutgoingItem(m.customer, _label(m), amount, "sent", "Sent (test mode)" if m.status == "dryrun" else "Sent"))
        elif m.status == FAILED:
            items.append(OutgoingItem(m.customer, _label(m), amount, "failed", f"Failed: {m.error or 'unknown error'}"))
        elif m.status == SKIPPED:
            items.append(OutgoingItem(m.customer, _label(m), amount, "skipped", f"Not sent: {m.error}"))
        elif m.status == CANCELLED:
            items.append(OutgoingItem(m.customer, _label(m), amount, "cancelled", f"Not needed: {(m.error or '').lower()}"))
        elif m.status == QUEUED and m.attempts:
            items.append(OutgoingItem(m.customer, _label(m), amount, "waiting",
                                      f"Retrying (tried {m.attempts} of {MAX_ATTEMPTS}): {m.error}"))
        elif m.status == QUEUED:
            state, note = _waiting_note(status, now, m.scheduled_for)
            if m.kind == KIND_ONE_OFF and m.note:
                note += f" · {m.note}"
            items.append(OutgoingItem(m.customer, _label(m), amount, state, note))

    for p in plan_reminders(db, today):
        if p.existing:
            continue
        label = ui.tone(p.rule.template_name)
        if p.skip_reason:
            items.append(OutgoingItem(p.customer, label, p.total, "skipped", f"Won't send: {p.skip_reason}"))
        elif now.hour < status.run_hour and not status.paused:
            items.append(OutgoingItem(p.customer, label, p.total, "waiting", f"Sends at {status.run_hour}:00"))
        else:
            state, note = _waiting_note(status, now, None)
            items.append(OutgoingItem(p.customer, label, p.total, state, note))

    order = {"failed": 0, "waiting": 1, "paused": 1, "sent": 2, "skipped": 3, "cancelled": 4}
    return sorted(items, key=lambda i: (order[i.state], i.customer.display_name.lower()))


def _missed_promises(db: Session, today: date, balances: dict[int, Decimal]) -> list[MissedPromise]:
    """Customers who promised to pay by a date that has passed, still owe, and haven't paid anything since."""
    promises = db.scalars(
        select(Message).options(joinedload(Message.customer))
        .where(Message.kind == KIND_ONE_OFF, Message.is_promise.is_(True), Message.run_date < today,
               Message.run_date >= today - timedelta(days=PROMISE_FOLLOW_UP_DAYS))
        .order_by(Message.run_date)
    ).all()
    out = []
    for m in promises:
        owed = balances.get(m.customer_id, Decimal(0))
        paid_since = db.scalar(select(Receipt.id).where(
            Receipt.customer_id == m.customer_id, Receipt.source != OPENING, Receipt.created_at >= m.created_at).limit(1))
        if owed > 0 and not paid_since:
            out.append(MissedPromise(m, owed))
    return out


def _invites_without_catalogue(db: Session) -> int:
    from app.reminders.followups import current_catalogue

    if current_catalogue(db) is not None:
        return 0
    return db.scalar(select(func.count(Message.id)).where(Message.kind == "reorder", Message.status == QUEUED)) or 0


def buckets(invoices: list[Invoice], today: date) -> dict[str, Decimal]:
    b = {"Not due yet": Decimal(0), "1-7 days late": Decimal(0), "8-15 days late": Decimal(0),
         "16-30 days late": Decimal(0), "Over 30 days late": Decimal(0)}
    for inv in invoices:
        d = inv.days_overdue(today)
        key = ("Not due yet" if d <= 0 else "1-7 days late" if d <= 7 else "8-15 days late" if d <= 15
               else "16-30 days late" if d <= 30 else "Over 30 days late")
        b[key] += inv.balance
    return b


def build_today(db: Session, now: datetime) -> TodayView:
    today = now.date()
    unpaid = [
        i for i in db.scalars(
            select(Invoice).options(joinedload(Invoice.customer)).where(Invoice.status.in_(UNPAID_STATUSES))
        ).all()
        if i.balance > 0
    ]
    per_customer: dict[int, dict] = {}
    for i in unpaid:
        r = per_customer.setdefault(i.customer_id, {"customer": i.customer, "total": Decimal(0), "overdue": False})
        r["total"] += i.balance
        r["overdue"] |= i.days_overdue(today) > 0
    balances = {cid: r["total"] for cid, r in per_customer.items()}

    # Money received in the last 7 days: typed in here or newly shown as paid in Busy (not opening history)
    collected = db.scalar(
        select(func.coalesce(func.sum(Receipt.amount), 0))
        .where(Receipt.received_on > today - timedelta(days=7), Receipt.received_on <= today, Receipt.source != OPENING)
    )
    reply_q = select(InboundMessage).where(InboundMessage.handled.is_(False))
    replies = db.scalars(reply_q.options(joinedload(InboundMessage.customer)).order_by(InboundMessage.id.desc()).limit(5)).all()
    replies_count = db.scalar(select(func.count()).select_from(reply_q.subquery()))
    failed = db.scalars(
        select(Message).options(joinedload(Message.customer))
        .where(Message.status == FAILED, Message.run_date > today - timedelta(days=7))
        .order_by(Message.id.desc()).limit(5)
    ).all()
    to_call = call_list(db, today=today, min_days_overdue=CALL_AFTER_DAYS)
    status = sending_status(db)

    return TodayView(
        today=today,
        collected_7d=Decimal(collected or 0),
        due_total=sum((i.balance for i in unpaid), Decimal(0)),
        overdue_total=sum((i.balance for i in unpaid if i.days_overdue(today) > 0), Decimal(0)),
        customers_due=len(per_customer),
        customers_overdue=sum(1 for r in per_customer.values() if r["overdue"]),
        replies=list(replies),
        replies_count=replies_count or 0,
        failed=list(failed),
        promises=_missed_promises(db, today, outstanding(db)),
        to_call=to_call[:3],
        to_call_count=len(to_call),
        invites_without_catalogue=_invites_without_catalogue(db),
        no_phone_count=sum(1 for r in per_customer.values() if not r["customer"].phone),
        outgoing=_outgoing(db, status, now, balances),
        status=status,
        last_import=db.scalar(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(1)),
        buckets=buckets(unpaid, today),
        top=sorted(per_customer.values(), key=lambda r: -r["total"])[:10],
    )
