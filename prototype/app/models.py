from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.clock import now as _now
from app.db import Base

# Invoice statuses
OPEN = "open"
PARTIAL = "partial"
PAID = "paid"
DISPUTED = "disputed"
UNPAID_STATUSES = (OPEN, PARTIAL)
OWING_STATUSES = (OPEN, PARTIAL, DISPUTED)  # bills that still count as money owed

# Receipt sources
MANUAL = "manual"    # typed into the app by the owner
IMPORT = "import"    # Busy showed more paid than the app knew about
OPENING = "opening"  # paid amount already in Busy when the app first saw the bill (not new money)


def fiscal_year_of(d: date) -> str:
    """Indian financial year (April-March): 2026-11-18 -> '2026-27', 2027-02-01 -> '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _invoice_fy(context) -> str:
    return fiscal_year_of(context.get_current_parameters()["invoice_date"])


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str | None] = mapped_column(String(50), unique=True)  # party code from billing software
    name: Mapped[str] = mapped_column(String(200))
    shop_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164 without "+", e.g. 919876543210
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    # Separate, explicit agreement to receive catalogues, offers and reorder invites (marketing messages)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    marketing_consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    marketing_consent_source: Mapped[str | None] = mapped_column(String(200))  # e.g. "told the owner in person"
    reminders_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_days: Mapped[int] = mapped_column(Integer, default=15)
    salesman: Mapped[str | None] = mapped_column(String(100))
    area: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")
    receipts: Mapped[list["Receipt"]] = relationship(back_populates="customer")

    @property
    def display_name(self) -> str:
        return self.shop_name or self.name


class Invoice(Base):
    __tablename__ = "invoices"
    # Busy restarts bill numbers every financial year, so a bill is identified by number + year.
    __table_args__ = (UniqueConstraint("invoice_no", "fiscal_year", name="uq_invoice_no_fy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_no: Mapped[str] = mapped_column(String(50), index=True)
    fiscal_year: Mapped[str] = mapped_column(String(7), default=_invoice_fy)  # e.g. "2026-27"
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Total of the payments allocated to this bill (kept in step by app.payments).
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # What Busy said was paid on the last upload. Lower than amount_paid = Busy not updated yet.
    busy_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default=OPEN, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(100))  # import batch / sync id
    # The bill PDF exported from Busy, if the owner uploaded it (otherwise a summary PDF is generated)
    bill_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))

    customer: Mapped[Customer] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice")

    @property
    def balance(self) -> Decimal:
        return Decimal(self.amount) - Decimal(self.amount_paid or 0)

    @property
    def busy_behind(self) -> bool:
        """Payments were recorded here that Busy doesn't show yet."""
        return self.busy_paid is not None and Decimal(self.busy_paid) < Decimal(self.amount_paid or 0)

    def days_overdue(self, today: date) -> int:
        return (today - self.due_date).days

    def refresh_status(self) -> None:
        if self.status == DISPUTED and self.balance > 0:
            return
        if self.balance <= 0:
            self.status = PAID
        elif self.amount_paid and self.amount_paid > 0:
            self.status = PARTIAL
        else:
            self.status = OPEN


class Receipt(Base):
    """Money received from a customer. It is split across bills by Payment rows; anything not
    split yet is the customer's credit (see app.payments.customer_credit)."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    received_on: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="cash")  # cash / upi / cheque / bank / busy
    note: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20), default=MANUAL)  # manual / import / opening
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    customer: Mapped[Customer] = relationship(back_populates="receipts")
    payments: Mapped[list["Payment"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")

    @property
    def allocated(self) -> Decimal:
        return sum((Decimal(p.amount) for p in self.payments), Decimal(0))

    @property
    def unallocated(self) -> Decimal:
        return Decimal(self.amount) - self.allocated


class Payment(Base):
    """The part of a receipt that pays one bill."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    receipt: Mapped[Receipt] = relationship(back_populates="payments")
    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class CustomerEvent(Base):
    """Things that happened to a customer that later steps act on, e.g. 'fully_paid' -> thank-you message."""

    __tablename__ = "customer_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # e.g. the payment that cleared the dues
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime)

    customer: Mapped[Customer] = relationship()


class ReminderRule(Base):
    """A step of the reminder schedule: remind when a bill is offset_days past its due date
    (negative = before). If a day is missed, the step still goes out within GRACE_DAYS."""

    __tablename__ = "reminder_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    offset_days: Mapped[int] = mapped_column(Integer, unique=True)
    template_name: Mapped[str] = mapped_column(String(100))  # Meta-approved template name (the tone)
    notify_salesman: Mapped[bool] = mapped_column(Boolean, default=False)
    escalate_to_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# Message kinds
KIND_REMINDER = "reminder"  # automatic, from the schedule
KIND_ONE_OFF = "one_off"    # the owner asked for a reminder on a date and time
KIND_MANUAL = "manual"      # the owner pressed "Send reminder now"
KIND_BILL = "bill"          # a bill PDF (new bill, or the owner pressed "Send bill")
KIND_THANKS = "thanks"      # thank-you once a customer has paid everything
KIND_REORDER = "reorder"    # invitation to order again, with the catalogue (marketing)

# Message statuses
QUEUED = "queued"        # waiting to go out (scheduled_for may be later), or waiting to retry
SENT_STATUSES = ("sent", "delivered", "read", "dryrun")
FAILED = "failed"        # gave up after the last retry
SKIPPED = "skipped"      # not sent: no number, opted out, paused customer...
CANCELLED = "cancelled"  # nothing owed any more, or the owner cancelled it


class Message(Base):
    """Every WhatsApp message the app sends or will send (the outbox)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default=KIND_REMINDER, index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("reminder_rules.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))  # for bill messages
    template_name: Mapped[str | None] = mapped_column(String(100))
    run_date: Mapped[date] = mapped_column(Date, index=True)  # the day it is for
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # not before this time
    invoice_nos: Mapped[str] = mapped_column(Text, default="")  # bills mentioned, comma separated
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    message_preview: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str | None] = mapped_column(String(200))  # the owner's note on a one-off reminder
    is_promise: Mapped[bool] = mapped_column(Boolean, default=False)  # customer promised to pay by this date
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    provider_msg_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Stops duplicates, e.g. "auto:<customer>:<date>" = at most one automatic reminder a day
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    customer: Mapped[Customer] = relationship()
    rule: Mapped[ReminderRule | None] = relationship()
    invoice: Mapped[Invoice | None] = relationship()

    @property
    def is_sent(self) -> bool:
        return self.status in SENT_STATUSES


class ReminderStep(Base):
    """Which schedule step each bill has already been reminded for (each step goes out once per bill)."""

    __tablename__ = "reminder_steps"
    __table_args__ = (UniqueConstraint("invoice_id", "rule_id", name="uq_step_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class InboundMessage(Base):
    """Replies customers send back on WhatsApp (e.g. 'paid', 'call me')."""

    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    phone: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)

    customer: Mapped[Customer | None] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    rows_ok: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    marked_paid: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AppSetting(Base):
    """Owner-editable settings (send time, window, pause...). Read through app.settings, not directly."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class StoredFile(Base):
    """A file kept in the database (bill PDFs from Busy, later catalogues and photos).

    The free host wipes its disk on every restart, so files live here; read and write them through
    app.storage, which can later be pointed at cloud storage without touching the rest of the app.
    """

    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)  # e.g. "bill_pdf"
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
