"""Payments: split across bills, credit, undo, Busy upload merge rules, financial years, fully-paid events."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.importers.outstanding import import_outstanding
from app.models import Customer, CustomerEvent, Invoice, Payment, Receipt, fiscal_year_of
from app.payments import (
    FULLY_PAID,
    PaymentError,
    apply_credit,
    customer_credit,
    outstanding,
    parse_money,
    record_payment,
    undo_receipt,
)

TODAY = date(2026, 11, 10)
D = Decimal


def cust(db, name="Sharma Store"):
    c = Customer(name=name, shop_name=name, phone="919800000001")
    db.add(c)
    db.flush()
    return c


def bill(db, c, no, amount, days_ago=20, paid=0, status=None):
    inv = Invoice(customer=c, invoice_no=no, invoice_date=TODAY - timedelta(days=days_ago),
                  due_date=TODAY - timedelta(days=days_ago - 15), amount=D(amount), amount_paid=D(paid))
    inv.refresh_status()
    if status:
        inv.status = status
    db.add(inv)
    db.commit()
    return inv


def events(db, c):
    return db.query(CustomerEvent).filter_by(customer_id=c.id, kind=FULLY_PAID).count()


# ---------- recording payments ----------

def test_split_across_two_bills_and_keep_the_rest_as_credit(db):
    c = cust(db)
    a, b = bill(db, c, "A", 1000), bill(db, c, "B", 2000)
    r = record_payment(db, c, D(2500), TODAY, "upi", "cheque 123", {a.id: D(1000), b.id: D(1200)})
    assert (a.status, a.balance) == ("paid", 0)
    assert (b.status, b.balance) == ("partial", D(800))
    assert r.allocated == D(2200) and r.unallocated == D(300)
    assert customer_credit(db, c.id) == D(300)
    assert outstanding(db, [c.id]) == {c.id: D(800)}


def test_nothing_split_means_all_credit(db):
    c = cust(db)
    a = bill(db, c, "A", 1000)
    record_payment(db, c, D(500), TODAY, allocations={})
    assert a.balance == D(1000) and customer_credit(db, c.id) == D(500)


@pytest.mark.parametrize("amount,allocs,message", [
    (D(500), {"A": D(600)}, "add up to more than"),
    (D(5000), {"A": D(1500)}, "only has ₹1,000.00 left"),
    (D(0), {}, "Enter the amount"),
])
def test_payment_mistakes_are_refused_and_change_nothing(db, amount, allocs, message):
    c = cust(db)
    a = bill(db, c, "A", 1000)
    with pytest.raises(PaymentError, match=message):
        record_payment(db, c, amount, TODAY, allocations={a.id: v for v in allocs.values()})
    db.rollback()
    assert db.query(Receipt).count() == 0 and db.get(Invoice, a.id).amount_paid == 0


def test_cannot_pay_another_customers_bill_or_a_paid_bill(db):
    c, other = cust(db), cust(db, "Other")
    theirs = bill(db, other, "X", 1000)
    done = bill(db, c, "P", 1000, paid=1000)
    with pytest.raises(PaymentError, match="doesn't belong"):
        record_payment(db, c, D(100), TODAY, allocations={theirs.id: D(100)})
    db.rollback()
    with pytest.raises(PaymentError, match="already paid"):
        record_payment(db, c, D(100), TODAY, allocations={done.id: D(100)})


def test_unknown_payment_mode_refused(db):
    c = cust(db)
    with pytest.raises(PaymentError):
        record_payment(db, c, D(100), TODAY, mode="bitcoin")


@pytest.mark.parametrize("text,expected", [("12,500", D("12500.00")), ("₹ 1500.5", D("1500.50")), ("", None), ("  ", None)])
def test_parse_money(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", ["abc", "-5"])
def test_parse_money_rejects_nonsense(text):
    with pytest.raises(PaymentError):
        parse_money(text)


# ---------- credit and undo ----------

def test_apply_credit_later_uses_oldest_receipts_first(db):
    c = cust(db)
    a = bill(db, c, "A", 1000)
    old = record_payment(db, c, D(300), TODAY - timedelta(days=5))
    new = record_payment(db, c, D(400), TODAY)
    apply_credit(db, c, {a.id: D(500)})
    assert a.amount_paid == D(500)
    assert (old.unallocated, new.unallocated) == (D(0), D(200))
    assert customer_credit(db, c.id) == D(200)


def test_cannot_use_more_credit_than_available(db):
    c = cust(db)
    a = bill(db, c, "A", 1000)
    record_payment(db, c, D(100), TODAY)
    with pytest.raises(PaymentError, match="more than the customer's credit"):
        apply_credit(db, c, {a.id: D(150)})


def test_undo_puts_bills_back(db):
    c = cust(db)
    a, b = bill(db, c, "A", 1000), bill(db, c, "B", 500, paid=100)
    r = record_payment(db, c, D(1400), TODAY, allocations={a.id: D(1000), b.id: D(400)})
    assert a.status == b.status == "paid"
    undo_receipt(db, r)
    assert (a.amount_paid, a.status) == (D(0), "open")
    assert (b.amount_paid, b.status) == (D(100), "partial")
    assert db.query(Receipt).count() == 0 and db.query(Payment).count() == 0


def test_busy_figures_cannot_be_undone(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    import_outstanding(db, "d2.csv", CSV_BUSY_HIGHER.encode(), today=TODAY)
    r = db.scalar(select(Receipt).where(Receipt.source == "import"))
    with pytest.raises(PaymentError, match="Only payments entered here"):
        undo_receipt(db, r)


# ---------- fully paid events ----------

def test_fully_paid_event_once_per_payoff(db):
    c = cust(db)
    a, b = bill(db, c, "A", 1000), bill(db, c, "B", 500)
    record_payment(db, c, D(1000), TODAY, allocations={a.id: D(1000)})
    assert events(db, c) == 0  # B still owed
    record_payment(db, c, D(500), TODAY, allocations={b.id: D(500)})
    assert events(db, c) == 1
    record_payment(db, c, D(200), TODAY)  # extra money as credit: still paid up, no new event
    assert events(db, c) == 1
    n = bill(db, c, "N", 300)  # new order...
    record_payment(db, c, D(300), TODAY, allocations={n.id: D(300)})  # ...paid off again
    assert events(db, c) == 2


def test_disputed_bill_still_counts_as_owed(db):
    c = cust(db)
    a = bill(db, c, "A", 1000)
    bill(db, c, "D", 400, status="disputed")
    record_payment(db, c, D(1000), TODAY, allocations={a.id: D(1000)})
    assert events(db, c) == 0


def test_paying_a_disputed_bill_in_full_marks_it_paid(db):
    c = cust(db)
    d = bill(db, c, "D", 400, status="disputed")
    record_payment(db, c, D(400), TODAY, allocations={d.id: D(400)})
    assert d.status == "paid" and events(db, c) == 1


# ---------- Busy uploads ----------

HEAD = "Bill No,Party Name,Bill Date,Bill Amount,Pending Amount\n"
CSV_DAY1 = HEAD + "INV1,Sharma Store,01-10-2026,10000,10000\nINV2,Sharma Store,05-10-2026,4000,1500\n"
CSV_BUSY_HIGHER = HEAD + "INV1,Sharma Store,01-10-2026,10000,7000\nINV2,Sharma Store,05-10-2026,4000,1500\n"


def _inv(db, no):
    return db.scalar(select(Invoice).where(Invoice.invoice_no == no))


def _sources(db):
    return sorted((r.source, r.amount) for r in db.scalars(select(Receipt)).all())


def test_first_upload_records_busy_paid_as_opening_history(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    inv2 = _inv(db, "INV2")
    assert (inv2.amount_paid, inv2.busy_paid, inv2.status) == (D(2500), D(2500), "partial")
    assert _sources(db) == [("opening", D(2500))]


def test_busy_shows_more_paid_adds_the_difference(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    import_outstanding(db, "d2.csv", CSV_BUSY_HIGHER.encode(), today=TODAY + timedelta(days=1))
    inv1 = _inv(db, "INV1")
    assert inv1.amount_paid == D(3000) and inv1.balance == D(7000)
    assert ("import", D(3000)) in _sources(db)


def test_busy_shows_less_paid_keeps_the_owners_payment(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    c = db.scalar(select(Customer))
    inv1 = _inv(db, "INV1")
    record_payment(db, c, D(4000), TODAY, allocations={inv1.id: D(4000)})
    res = import_outstanding(db, "d2.csv", CSV_DAY1.encode(), today=TODAY + timedelta(days=1))  # Busy not updated
    assert inv1.amount_paid == D(4000) and inv1.balance == D(6000)  # never lowered
    assert inv1.busy_behind and res.busy_behind == ["INV1"]
    # Busy catches up: flag clears, nothing double-counted
    caught_up = HEAD + "INV1,Sharma Store,01-10-2026,10000,6000\nINV2,Sharma Store,05-10-2026,4000,1500\n"
    res = import_outstanding(db, "d3.csv", caught_up.encode(), today=TODAY + timedelta(days=2))
    assert inv1.amount_paid == D(4000) and not inv1.busy_behind and res.busy_behind == []


def test_same_upload_twice_adds_nothing(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    import_outstanding(db, "d2.csv", CSV_BUSY_HIGHER.encode(), today=TODAY)
    before = _sources(db)
    import_outstanding(db, "d2-again.csv", CSV_BUSY_HIGHER.encode(), today=TODAY)
    assert _sources(db) == before


def test_bill_missing_from_full_list_is_paid_via_import_receipt(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    only_inv1 = HEAD + "INV1,Sharma Store,01-10-2026,10000,10000\n"
    import_outstanding(db, "d2.csv", only_inv1.encode(), full_outstanding_report=True, today=TODAY)
    inv2 = _inv(db, "INV2")
    assert inv2.status == "paid" and inv2.amount_paid == D(4000)
    assert ("import", D(1500)) in _sources(db)


def test_upload_that_clears_a_customer_records_fully_paid_once(db):
    import_outstanding(db, "d1.csv", CSV_DAY1.encode(), today=TODAY)
    c = db.scalar(select(Customer))
    other = HEAD + "X1,Someone Else,01-10-2026,500,500\n"
    import_outstanding(db, "d2.csv", other.encode(), full_outstanding_report=True, today=TODAY)
    assert events(db, c) == 1
    import_outstanding(db, "d3.csv", other.encode(), full_outstanding_report=True, today=TODAY)
    assert events(db, c) == 1


def test_first_upload_of_an_already_paid_bill_is_not_a_payoff(db):
    import_outstanding(db, "d1.csv", (HEAD + "OLD1,New Party,01-10-2026,900,0\n").encode(), today=TODAY)
    c = db.scalar(select(Customer).where(Customer.name == "New Party"))
    assert _inv(db, "OLD1").status == "paid" and events(db, c) == 0


# ---------- financial years ----------

@pytest.mark.parametrize("d,fy", [
    (date(2026, 4, 1), "2026-27"), (date(2026, 3, 31), "2025-26"), (date(2027, 2, 1), "2026-27"), (date(1999, 12, 1), "1999-00"),
])
def test_fiscal_year_of(d, fy):
    assert fiscal_year_of(d) == fy


def test_same_bill_number_in_two_financial_years_are_different_bills(db):
    two_years = HEAD + "101,Sharma Store,15-03-2026,1000,1000\n101,Gupta Kirana,15-04-2026,2500,2500\n"
    res = import_outstanding(db, "fy.csv", two_years.encode(), today=TODAY)
    assert res.batch.rows_ok == 2
    bills = {(i.fiscal_year, i.customer.name, i.amount) for i in db.scalars(select(Invoice)).all()}
    assert bills == {("2025-26", "Sharma Store", D(1000)), ("2026-27", "Gupta Kirana", D(2500))}
    # next upload updates the right one
    import_outstanding(db, "fy2.csv", (HEAD + "101,Gupta Kirana,15-04-2026,2500,500\n").encode(),
                       full_outstanding_report=False, today=TODAY)
    new = db.scalar(select(Invoice).where(Invoice.fiscal_year == "2026-27"))
    old = db.scalar(select(Invoice).where(Invoice.fiscal_year == "2025-26"))
    assert new.balance == D(500) and old.balance == D(1000)
