"""Small display helpers used by the templates (registered as Jinja filters in app.routes.pages)."""

from datetime import date, datetime
from decimal import Decimal

# Meta template name -> how the owner thinks about it
TONES = {
    "payment_reminder_upcoming": "Friendly heads-up",
    "payment_due_today": "Due today",
    "payment_overdue": "Polite reminder",
    "payment_overdue_firm": "Firm reminder",
    "payment_final_notice": "Final notice",
}

STATUS_LABELS = {
    "dryrun": "Test mode",
    "sent": "Sent",
    "delivered": "Delivered",
    "read": "Read",
    "failed": "Failed",
    "skipped": "Not sent",
    "cancelled": "Not needed",
    "queued": "Waiting",
    "open": "Unpaid",
    "partial": "Part paid",
    "paid": "Paid",
    "disputed": "Disputed",
}


def inr(value, decimals: bool | None = None) -> str:
    """Indian digit grouping: 1250000 -> '12,50,000'. Paise shown only when present (or decimals=True)."""
    d = Decimal(value or 0).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    d = abs(d)
    whole, paise = divmod(d, 1)
    digits = str(int(whole))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    show_paise = decimals if decimals is not None else paise != 0
    return f"{sign}{digits}.{int(paise * 100):02d}" if show_paise else f"{sign}{digits}"


def when(value: date | datetime | None, today: date | None = None) -> str:
    """'Today', 'Yesterday', '18 Nov', '18 Nov 2025' (year only when not the current year)."""
    if value is None:
        return ""
    d = value.date() if isinstance(value, datetime) else value
    if today is not None:
        if d == today:
            return "Today"
        if (today - d).days == 1:
            return "Yesterday"
        if (d - today).days == 1:
            return "Tomorrow"
    same_year = today is None or d.year == today.year
    return f"{d.day} {d:%b}" if same_year else f"{d.day} {d:%b %Y}"


def when_phrase(value: date | datetime | None, today: date, prep: str = "on") -> str:
    """For the middle of a sentence: 'today', 'tomorrow', 'yesterday', else 'on 21 Nov' (or 'by 21 Nov')."""
    text = when(value, today)
    return text.lower() if text in ("Today", "Tomorrow", "Yesterday") else f"{prep} {text}"


def days_text(offset: int) -> str:
    """Reminder rule offset in plain words: -3 -> '3 days before due', 0 -> 'On the due date'."""
    if offset == 0:
        return "On the due date"
    n = abs(offset)
    unit = "day" if n == 1 else "days"
    return f"{n} {unit} {'before' if offset < 0 else 'after'} due"


def overdue_text(days: int) -> str:
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} overdue"
    if days == 0:
        return "Due today"
    return f"Due in {-days} day{'s' if days != -1 else ''}"


def tone(template_name: str | None) -> str:
    if not template_name:
        return "Reminder"
    return TONES.get(template_name, template_name.replace("_", " ").capitalize())


def message_label(msg) -> str:
    """How the owner thinks of a message: who asked for it, or its tone."""
    if msg.kind == "manual":
        return "Sent by you"
    if msg.kind == "one_off":
        return "Promise reminder" if msg.is_promise else "Your scheduled reminder"
    if msg.kind == "thanks":
        return "Thank-you"
    if msg.kind == "reorder":
        return "Reorder invite"
    if msg.kind == "bill":
        return f"Bill {msg.invoice.invoice_no}" if msg.invoice is not None else "Bill"
    return tone(msg.template_name)


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", (status or "").capitalize())
