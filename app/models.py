from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
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


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str | None] = mapped_column(String(50), unique=True)  # party code from billing software
    name: Mapped[str] = mapped_column(String(200))
    shop_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164 without "+", e.g. 919876543210
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    reminders_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_days: Mapped[int] = mapped_column(Integer, default=15)
    salesman: Mapped[str | None] = mapped_column(String(100))
    area: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")

    @property
    def display_name(self) -> str:
        return self.shop_name or self.name


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_no: Mapped[str] = mapped_column(String(50), unique=True)
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default=OPEN, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(100))  # import batch / sync id

    customer: Mapped[Customer] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice")

    @property
    def balance(self) -> Decimal:
        return Decimal(self.amount) - Decimal(self.amount_paid or 0)

    def days_overdue(self, today: date) -> int:
        return (today - self.due_date).days

    def refresh_status(self) -> None:
        if self.status == DISPUTED:
            return
        if self.balance <= 0:
            self.status = PAID
        elif self.amount_paid and self.amount_paid > 0:
            self.status = PARTIAL
        else:
            self.status = OPEN


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    paid_on: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(20), default="cash")  # cash / upi / cheque / bank
    note: Mapped[str | None] = mapped_column(String(200))

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class ReminderRule(Base):
    """Send a reminder when (today - due_date) == offset_days."""

    __tablename__ = "reminder_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    offset_days: Mapped[int] = mapped_column(Integer, unique=True)
    template_name: Mapped[str] = mapped_column(String(100))  # Meta-approved template name
    notify_salesman: Mapped[bool] = mapped_column(Boolean, default=False)
    escalate_to_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    # One message per customer per rule per day; also stops a second run on the same day re-sending.
    __table_args__ = (UniqueConstraint("customer_id", "rule_id", "run_date", name="uq_reminder_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id"))
    run_date: Mapped[date] = mapped_column(Date)
    invoice_nos: Mapped[str] = mapped_column(Text)  # comma separated invoice numbers covered
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    message_preview: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    provider_msg_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/sent/delivered/read/failed/dryrun
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    customer: Mapped[Customer] = relationship()
    rule: Mapped[ReminderRule] = relationship()


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
