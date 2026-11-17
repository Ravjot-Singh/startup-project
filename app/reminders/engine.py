"""Daily reminder run.

1. Find unpaid invoices whose (today - due_date) equals an active rule's offset_days.
2. Group by customer: one message per customer, using the most severe rule hit today, and
   quoting the customer's TOTAL unpaid balance (not just the invoice that triggered it).
3. Skip customers who are opted out / paused / have no phone / paid something today / were
   already sent this rule today.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app import config
from app.models import UNPAID_STATUSES, Customer, Invoice, Payment, ReminderLog, ReminderRule
from app.whatsapp.provider import WhatsAppProvider
from app.whatsapp.templates import render

MAX_INVOICES_IN_MESSAGE = 3


@dataclass
class RunSummary:
    run_date: date
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    already_done: int = 0
    outside_window: bool = False
    details: list[str] = field(default_factory=list)


def fmt_amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def upi_link(amount: Decimal, note: str) -> str:
    if not config.BUSINESS_UPI_ID:
        return "contact us for payment details"
    return (
        f"upi://pay?pa={quote(config.BUSINESS_UPI_ID)}&pn={quote(config.BUSINESS_NAME)}"
        f"&am={amount:.2f}&cu=INR&tn={quote(note)}"
    )


def build_params(customer: Customer, invoices: list[Invoice]) -> tuple[list[str], Decimal]:
    invoices = sorted(invoices, key=lambda i: i.due_date)
    total = sum((i.balance for i in invoices), Decimal("0"))
    nos = [i.invoice_no for i in invoices[:MAX_INVOICES_IN_MESSAGE]]
    inv_text = ", ".join(nos)
    if len(invoices) > MAX_INVOICES_IN_MESSAGE:
        inv_text += f" +{len(invoices) - MAX_INVOICES_IN_MESSAGE} more"
    params = [
        customer.display_name,
        config.BUSINESS_NAME,
        fmt_amount(total),
        inv_text,
        invoices[0].due_date.strftime("%d-%b-%Y"),
        upi_link(total, f"Dues {customer.code or customer.id}"),
    ]
    return params, total


def _skip_reason(db: Session, customer: Customer, today: date) -> str | None:
    if not customer.is_active:
        return "customer inactive"
    if not customer.whatsapp_opt_in:
        return "not opted in to WhatsApp"
    if customer.reminders_paused:
        return "reminders paused"
    if not customer.phone:
        return "no phone number"
    paid_today = db.scalar(
        select(Payment.id).join(Invoice).where(Invoice.customer_id == customer.id, Payment.paid_on == today).limit(1)
    )
    if paid_today:
        return "payment recorded today"
    return None


def run_reminders(
    db: Session,
    provider: WhatsAppProvider,
    today: date | None = None,
    now: datetime | None = None,
    enforce_window: bool = True,
) -> RunSummary:
    today = today or date.today()
    now = now or datetime.now()
    summary = RunSummary(run_date=today)

    if enforce_window and not (config.SEND_WINDOW_START <= now.hour < config.SEND_WINDOW_END):
        summary.outside_window = True
        summary.details.append(
            f"Not sending: {now:%H:%M} is outside the {config.SEND_WINDOW_START}:00-{config.SEND_WINDOW_END}:00 window"
        )
        return summary

    rules = {r.offset_days: r for r in db.scalars(select(ReminderRule).where(ReminderRule.is_active)).all()}
    if not rules:
        summary.details.append("No active reminder rules")
        return summary

    unpaid = db.scalars(
        select(Invoice).options(joinedload(Invoice.customer)).where(Invoice.status.in_(UNPAID_STATUSES))
    ).all()

    by_customer: dict[int, list[Invoice]] = defaultdict(list)
    triggered: set[int] = set()
    for inv in unpaid:
        if inv.balance <= 0:
            continue
        by_customer[inv.customer_id].append(inv)
        if inv.days_overdue(today) in rules:
            triggered.add(inv.customer_id)

    offsets = sorted(rules)
    for customer_id in triggered:
        invoices = by_customer[customer_id]
        # A bill hitting a rule today triggers the message, but its tone follows the OLDEST unpaid bill:
        # a customer with a 31-day-old bill should not get a friendly "due today" note for a new bill.
        worst = max(i.days_overdue(today) for i in invoices)
        rule = rules[max(o for o in offsets if o <= worst)]
        customer = invoices[0].customer
        existing = db.scalar(
            select(ReminderLog).where(
                ReminderLog.customer_id == customer_id,
                ReminderLog.rule_id == rule.id,
                ReminderLog.run_date == today,
            )
        )
        if existing:
            summary.already_done += 1
            continue

        params, total = build_params(customer, invoices)
        log = ReminderLog(
            customer_id=customer_id,
            rule_id=rule.id,
            run_date=today,
            invoice_nos=",".join(i.invoice_no for i in sorted(invoices, key=lambda i: i.due_date)),
            amount_due=total,
            message_preview=render(rule.template_name, params),
            attempts=0,
        )
        db.add(log)

        reason = _skip_reason(db, customer, today)
        if reason:
            log.status = "skipped"
            log.error = reason
            summary.skipped += 1
            summary.details.append(f"Skipped {customer.display_name}: {reason}")
            db.commit()
            continue

        result = None
        for _ in range(2):  # one retry
            log.attempts += 1
            result = provider.send_template(customer.phone, rule.template_name, params)
            if result.ok:
                break
        log.status = result.status if result.ok else "failed"
        log.provider_msg_id = result.message_id
        log.error = result.error
        log.sent_at = datetime.now() if result.ok else None
        if result.ok:
            summary.sent += 1
            summary.details.append(f"{rule.name} -> {customer.display_name} (Rs. {fmt_amount(total)})")
        else:
            summary.failed += 1
            summary.details.append(f"FAILED {customer.display_name}: {result.error}")
        db.commit()

    return summary


def call_list(db: Session, today: date | None = None, min_days_overdue: int = 15) -> list[dict]:
    """Customers whose oldest unpaid invoice is at least min_days_overdue late, worst first."""
    today = today or date.today()
    rows: dict[int, dict] = {}
    for inv in db.scalars(
        select(Invoice).options(joinedload(Invoice.customer)).where(Invoice.status.in_(UNPAID_STATUSES))
    ).all():
        if inv.balance <= 0:
            continue
        r = rows.setdefault(
            inv.customer_id, {"customer": inv.customer, "total": Decimal("0"), "max_overdue": -10**6, "count": 0}
        )
        r["total"] += inv.balance
        r["count"] += 1
        r["max_overdue"] = max(r["max_overdue"], inv.days_overdue(today))
    result = [r for r in rows.values() if r["max_overdue"] >= min_days_overdue]
    return sorted(result, key=lambda r: (-r["max_overdue"], -r["total"]))
