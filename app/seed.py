"""Default reminder rules (and optional demo data).

    python -m app.seed          # rules only
    python -m app.seed --demo   # rules + sample customers/invoices
"""

import sys
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import SessionLocal, init_db
from app.models import Customer, Invoice, ReminderRule

DEFAULT_RULES = [
    # name, offset_days, template, notify_salesman, escalate_to_owner
    ("Heads-up (3 days before due)", -3, "payment_reminder_upcoming", False, False),
    ("Due today", 0, "payment_due_today", False, False),
    ("Overdue 3 days", 3, "payment_overdue", False, False),
    ("Overdue 7 days", 7, "payment_overdue", False, False),
    ("Firm reminder (15 days)", 15, "payment_overdue_firm", True, False),
    ("Final notice (30 days)", 30, "payment_final_notice", True, True),
]


def seed_rules(db: Session) -> None:
    for name, offset, template, salesman, owner in DEFAULT_RULES:
        if db.scalar(select(ReminderRule).where(ReminderRule.offset_days == offset)) is None:
            db.add(
                ReminderRule(
                    name=name,
                    offset_days=offset,
                    template_name=template,
                    notify_salesman=salesman,
                    escalate_to_owner=owner,
                )
            )
    db.commit()


def seed_demo(db: Session, today: date | None = None) -> None:
    today = today or clock.today()
    if db.scalar(select(Customer).limit(1)):
        return
    people = [
        ("C001", "Sharma General Store", "919800000001", "Model Town", "Ravi"),
        ("C002", "Hotel Punjab Grill", "919800000002", "Civil Lines", "Ravi"),
        ("C003", "Gupta Kirana", "919800000003", "Sarabha Nagar", "Aman"),
        ("C004", "Fresh Mart", None, "Model Town", "Aman"),
    ]
    customers = []
    for code, name, phone, area, sm in people:
        c = Customer(code=code, name=name, shop_name=name, phone=phone, area=area, salesman=sm, credit_days=15)
        db.add(c)
        customers.append(c)
    # (customer index, invoice no, days overdue today, amount, paid)
    bills = [
        (0, "INV1001", -3, 8200, 0),
        (0, "INV0990", 3, 4500, 1500),
        (1, "INV1002", 0, 15750, 0),
        (1, "INV0950", 31, 22000, 0),
        (2, "INV0975", 15, 6300, 0),
        (3, "INV0980", 7, 3900, 0),
    ]
    for ci, no, overdue, amount, paid in bills:
        due = today - timedelta(days=overdue)
        inv = Invoice(
            customer=customers[ci],
            invoice_no=no,
            invoice_date=due - timedelta(days=15),
            due_date=due,
            amount=Decimal(amount),
            amount_paid=Decimal(paid),
        )
        inv.refresh_status()
        db.add(inv)
    db.commit()


if __name__ == "__main__":
    init_db()
    with SessionLocal() as db:
        seed_rules(db)
        if "--demo" in sys.argv:
            seed_demo(db)
    print("Seed complete.")
