"""Payments: the only place that changes what a bill has been paid.

* A Receipt is money received from a customer (amount, date, mode).
* The owner decides which bills it pays; each part is a Payment row. Whatever is not split
  onto bills stays with the customer as credit and can be applied to bills later.
* Busy uploads never lower a payment: if Busy shows more paid than the app knows, the
  difference is added as an 'import' receipt; if it shows less, the app keeps its own
  payments and flags the bill "Busy not updated yet".
* When a customer's dues go from something to nothing, a 'fully_paid' event is recorded
  once (it drives the thank-you message in a later phase).
"""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    IMPORT,
    MANUAL,
    OPENING,
    OWING_STATUSES,
    Customer,
    CustomerEvent,
    ImportBatch,
    Invoice,
    Payment,
    Receipt,
)

CENT = Decimal("0.01")
FULLY_PAID = "fully_paid"
MODES = {"cash": "Cash", "upi": "UPI", "cheque": "Cheque", "bank": "Bank transfer"}


class PaymentError(ValueError):
    """A payment the owner entered doesn't add up; the message is shown to them as-is."""


def parse_money(text: str | None) -> Decimal | None:
    """'12,500', '₹ 1500.5' -> Decimal; blank -> None; nonsense -> PaymentError."""
    cleaned = (text or "").replace(",", "").replace("₹", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned).quantize(CENT)
    except InvalidOperation:
        raise PaymentError(f"“{text}” is not an amount") from None
    if value < 0:
        raise PaymentError("Amounts can't be negative")
    return value


# ---------- reading balances ----------

def outstanding(db: Session, customer_ids: Iterable[int] | None = None) -> dict[int, Decimal]:
    """Money still owed per customer (unpaid, part-paid and disputed bills)."""
    stmt = (
        select(Invoice.customer_id, func.sum(Invoice.amount - Invoice.amount_paid))
        .where(Invoice.status.in_(OWING_STATUSES))
        .group_by(Invoice.customer_id)
    )
    if customer_ids is not None:
        stmt = stmt.where(Invoice.customer_id.in_(list(customer_ids)))
    return {cid: max(Decimal(total or 0), Decimal(0)) for cid, total in db.execute(stmt).all()}


def customer_credit(db: Session, customer_id: int) -> Decimal:
    """Money received from the customer that hasn't been put against any bill yet."""
    received = db.scalar(select(func.coalesce(func.sum(Receipt.amount), 0)).where(Receipt.customer_id == customer_id))
    allocated = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).join(Receipt).where(Receipt.customer_id == customer_id)
    )
    return Decimal(received or 0) - Decimal(allocated or 0)


def record_fully_paid(db: Session, before: dict[int, Decimal], customer_ids: Iterable[int],
                      amounts: dict[int, Decimal] | None = None) -> list[int]:
    """Add a 'fully_paid' event for each customer who owed money before and owes nothing now.
    amounts: the payment that cleared each customer's dues (shown in the thank-you message)."""
    ids = [cid for cid in set(customer_ids) if before.get(cid, Decimal(0)) > 0]
    if not ids:
        return []
    db.flush()
    after = outstanding(db, ids)
    cleared = [cid for cid in ids if after.get(cid, Decimal(0)) <= 0]
    for cid in cleared:
        db.add(CustomerEvent(customer_id=cid, kind=FULLY_PAID, amount=(amounts or {}).get(cid)))
    return cleared


# ---------- changing payments ----------

def _allocate(db: Session, receipt: Receipt, customer: Customer, allocations: dict[int, Decimal]) -> None:
    for invoice_id, amt in allocations.items():
        if amt <= 0:
            continue
        inv = db.get(Invoice, invoice_id)
        if inv is None or inv.customer_id != customer.id:
            raise PaymentError("That bill doesn't belong to this customer")
        if inv.status not in OWING_STATUSES or inv.balance <= 0:
            raise PaymentError(f"Bill {inv.invoice_no} is already paid")
        if amt > inv.balance:
            raise PaymentError(f"Bill {inv.invoice_no} only has ₹{inv.balance:,.2f} left; you entered ₹{amt:,.2f}")
        db.add(Payment(receipt=receipt, invoice=inv, amount=amt))
        inv.amount_paid = Decimal(inv.amount_paid or 0) + amt
        inv.refresh_status()


def _clean_allocations(allocations: dict[int, Decimal | None]) -> dict[int, Decimal]:
    return {iid: amt for iid, amt in allocations.items() if amt is not None and amt > 0}


def record_payment(
    db: Session,
    customer: Customer,
    amount: Decimal,
    received_on: date,
    mode: str = "cash",
    note: str | None = None,
    allocations: dict[int, Decimal | None] | None = None,
) -> Receipt:
    """Money received by the owner, split across the bills they chose. The rest becomes credit."""
    if amount is None or amount <= 0:
        raise PaymentError("Enter the amount received")
    if mode not in MODES:
        raise PaymentError("Choose how it was paid")
    allocs = _clean_allocations(allocations or {})
    if sum(allocs.values(), Decimal(0)) > amount:
        raise PaymentError(f"The bills add up to more than the ₹{amount:,.2f} received")
    before = outstanding(db, [customer.id])
    receipt = Receipt(customer=customer, amount=amount, received_on=received_on, mode=mode, note=note or None, source=MANUAL)
    db.add(receipt)
    _allocate(db, receipt, customer, allocs)
    record_fully_paid(db, before, [customer.id], {customer.id: amount})
    db.commit()
    return receipt


def apply_credit(db: Session, customer: Customer, allocations: dict[int, Decimal | None]) -> Decimal:
    """Put the customer's unused credit against bills (oldest receipts are used first)."""
    allocs = _clean_allocations(allocations)
    total = sum(allocs.values(), Decimal(0))
    if total <= 0:
        raise PaymentError("Enter how much credit to put against each bill")
    if total > customer_credit(db, customer.id):
        raise PaymentError("That's more than the customer's credit")
    before = outstanding(db, [customer.id])
    receipts = db.scalars(select(Receipt).where(Receipt.customer_id == customer.id).order_by(Receipt.received_on, Receipt.id)).all()
    for invoice_id, amt in allocs.items():
        remaining = amt
        for r in receipts:
            take = min(r.unallocated, remaining)
            if take > 0:
                _allocate(db, r, customer, {invoice_id: take})
                db.flush()
                remaining -= take
            if remaining <= 0:
                break
    record_fully_paid(db, before, [customer.id], {customer.id: total})
    db.commit()
    return total


def undo_receipt(db: Session, receipt: Receipt) -> None:
    """Remove a payment the owner typed by mistake; its bills go back to what they were."""
    if receipt.source != MANUAL:
        raise PaymentError("Only payments entered here can be undone. Busy's figures come from the next upload.")
    for p in receipt.payments:
        inv = p.invoice
        inv.amount_paid = Decimal(inv.amount_paid or 0) - Decimal(p.amount)
        if inv.status == "paid":
            inv.status = "open"
        inv.refresh_status()
    db.delete(receipt)
    db.commit()


# ---------- Busy uploads ----------

def apply_busy_paid(db: Session, inv: Invoice, busy_paid: Decimal, batch: ImportBatch, today: date, first_seen: bool) -> Decimal:
    """Bring a bill in line with what Busy says was paid, without ever lowering a payment.

    Returns the amount added. On the first upload of a bill, Busy's paid amount is history, not
    new money, so it is recorded as an 'opening' receipt (left out of 'collected' totals).
    """
    busy_paid = min(max(busy_paid, Decimal(0)), Decimal(inv.amount)).quantize(CENT)
    inv.busy_paid = busy_paid
    extra = busy_paid - Decimal(inv.amount_paid or 0)
    if extra > 0:
        receipt = Receipt(customer_id=inv.customer_id, amount=extra, received_on=today, mode="busy",
                          source=OPENING if first_seen else IMPORT, import_batch_id=batch.id)
        db.add(receipt)
        db.add(Payment(receipt=receipt, invoice=inv, amount=extra))
        inv.amount_paid = Decimal(inv.amount_paid or 0) + extra
    inv.refresh_status()
    return max(extra, Decimal(0))
