"""Everything the Today page shows, most important first:

1. Three numbers: collected in the last 7 days, due to you, overdue.
2. Needs you: replies to read, messages that failed, customers to call, customers we can't message.
3. Going out today: what the automatic run sends (or sent) today, and why someone is left out.
4. Details (folded away): ageing and biggest dues.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app import config, settings
from app.models import OPENING, UNPAID_STATUSES, ImportBatch, InboundMessage, Invoice, Receipt, ReminderLog
from app.reminders.engine import PlannedReminder, call_list, plan_reminders

CALL_AFTER_DAYS = 15
SENT_STATUSES = ("sent", "delivered", "read", "dryrun")


@dataclass
class OutgoingItem:
    plan: PlannedReminder
    state: str  # "sent" | "failed" | "skipped" | "waiting" | "paused"
    note: str


@dataclass
class SendingStatus:
    paused: bool
    test_mode: bool
    run_hour: int
    window_start: int
    window_end: int

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
    failed: list[ReminderLog]
    to_call: list[dict]
    to_call_count: int
    no_phone_count: int
    outgoing: list[OutgoingItem]
    status: SendingStatus
    last_import: ImportBatch | None
    buckets: dict[str, Decimal] = field(default_factory=dict)
    top: list[dict] = field(default_factory=list)

    @property
    def needs_count(self) -> int:
        """Number of things to look at, matching the rows shown (each group of customers is one row)."""
        return self.replies_count + len(self.failed) + (1 if self.to_call_count else 0) + (1 if self.no_phone_count else 0)

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
    )


def _outgoing(plan: list[PlannedReminder], status: SendingStatus, now: datetime) -> list[OutgoingItem]:
    items = []
    for p in plan:
        log = p.existing
        if log and log.status in SENT_STATUSES:
            items.append(OutgoingItem(p, "sent", "Sent" if log.status != "dryrun" else "Sent (test mode)"))
        elif log and log.status == "failed":
            items.append(OutgoingItem(p, "failed", f"Failed: {log.error or 'unknown error'}"))
        elif log and log.status == "skipped":
            items.append(OutgoingItem(p, "skipped", f"Not sent: {log.error}"))
        elif p.skip_reason:
            items.append(OutgoingItem(p, "skipped", f"Won't send: {p.skip_reason}"))
        elif status.paused:
            items.append(OutgoingItem(p, "paused", "Paused"))
        elif now.hour < status.run_hour:
            items.append(OutgoingItem(p, "waiting", f"Sends at {status.run_hour}:00"))
        elif not (status.window_start <= now.hour < status.window_end):
            items.append(OutgoingItem(p, "waiting", f"Outside sending hours, next chance {status.window_start}:00 tomorrow"))
        else:
            items.append(OutgoingItem(p, "waiting", "Sending in the next few minutes"))
    order = {"failed": 0, "waiting": 1, "paused": 1, "sent": 2, "skipped": 3}
    return sorted(items, key=lambda i: (order[i.state], i.plan.customer.display_name.lower()))


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

    # Money received in the last 7 days: typed in here or newly shown as paid in Busy (not opening history)
    collected = db.scalar(
        select(func.coalesce(func.sum(Receipt.amount), 0))
        .where(Receipt.received_on > today - timedelta(days=7), Receipt.received_on <= today, Receipt.source != OPENING)
    )
    reply_q = select(InboundMessage).where(InboundMessage.handled.is_(False))
    replies = db.scalars(reply_q.options(joinedload(InboundMessage.customer)).order_by(InboundMessage.id.desc()).limit(5)).all()
    replies_count = db.scalar(select(func.count()).select_from(reply_q.subquery()))
    failed = db.scalars(
        select(ReminderLog).options(joinedload(ReminderLog.customer))
        .where(ReminderLog.status == "failed", ReminderLog.run_date > today - timedelta(days=7))
        .order_by(ReminderLog.id.desc()).limit(5)
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
        to_call=to_call[:3],
        to_call_count=len(to_call),
        no_phone_count=sum(1 for r in per_customer.values() if not r["customer"].phone),
        outgoing=_outgoing(plan_reminders(db, today), status, now),
        status=status,
        last_import=db.scalar(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(1)),
        buckets=buckets(unpaid, today),
        top=sorted(per_customer.values(), key=lambda r: -r["total"])[:10],
    )
