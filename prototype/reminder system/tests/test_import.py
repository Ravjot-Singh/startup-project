import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.importers.outstanding import ImportError_, import_outstanding, normalize_phone
from app.models import Customer, Invoice

TODAY = date(2026, 11, 10)

CSV = """Bill No,Party Name,Bill Date,Due Date,Bill Amount,Pending Amount,Mobile,Area
INV101,Sharma General Store,01-10-2026,16-10-2026,"12,500.00","12,500.00",98765 43210,Model Town
INV102,Sharma General Store,05/10/2026,,4000,1500,,
INV103,Gupta Kirana,2026-10-20,2026-11-04,₹ 3200 Dr,3200,09812345678,Sarabha Nagar
INV104,Bad Row,not-a-date,,100,100,,
"""


def test_csv_import_creates_customers_and_invoices(db):
    res = import_outstanding(db, "outstanding.csv", CSV.encode(), today=TODAY)
    assert res.batch.rows_ok == 3 and res.batch.rows_failed == 1
    assert "Row 5" in res.errors[0]

    sharma = db.scalar(select(Customer).where(Customer.name == "Sharma General Store"))
    assert sharma.phone == "919876543210" and sharma.area == "Model Town"
    inv102 = db.scalar(select(Invoice).where(Invoice.invoice_no == "INV102"))
    assert inv102.amount_paid == Decimal("2500.00") and inv102.status == "partial"
    assert inv102.due_date == date(2026, 10, 20)  # bill date + default 15 credit days
    gupta = db.scalar(select(Customer).where(Customer.name == "Gupta Kirana"))
    assert gupta.phone == "919812345678"


def test_reimport_updates_and_marks_missing_bills_paid(db):
    import_outstanding(db, "day1.csv", CSV.encode(), today=TODAY)
    day2 = "Bill No,Party Name,Bill Date,Bill Amount,Pending Amount\nINV101,Sharma General Store,01-10-2026,12500,2500\n"
    res = import_outstanding(db, "day2.csv", day2.encode(), today=TODAY)
    assert res.batch.marked_paid == 2  # INV102, INV103 disappeared from the outstanding list
    statuses = {i.invoice_no: i.status for i in db.scalars(select(Invoice)).all()}
    assert statuses == {"INV101": "partial", "INV102": "paid", "INV103": "paid"}
    assert db.scalar(select(Invoice).where(Invoice.invoice_no == "INV101")).balance == Decimal("2500.00")


def test_partial_file_does_not_mark_others_paid(db):
    import_outstanding(db, "day1.csv", CSV.encode(), today=TODAY)
    day2 = "Bill No,Party Name,Bill Date,Bill Amount\nINV200,New Shop,01-11-2026,900\n"
    res = import_outstanding(db, "new.csv", day2.encode(), full_outstanding_report=False, today=TODAY)
    assert res.batch.marked_paid == 0
    assert db.scalar(select(Invoice).where(Invoice.invoice_no == "INV103")).status == "open"


def test_xlsx_with_title_rows(db):
    wb = Workbook()
    ws = wb.active
    ws.append(["ABC Frozen Foods"])
    ws.append(["Bills Receivable as on 10-11-2026"])
    ws.append([])
    ws.append(["Invoice No", "Customer Name", "Invoice Date", "Amount", "Balance", "Phone"])
    ws.append([5001, "Hotel Punjab Grill", datetime(2026, 10, 1), 15750, 15750, 9800000002])
    buf = io.BytesIO()
    wb.save(buf)
    res = import_outstanding(db, "report.xlsx", buf.getvalue(), today=TODAY)
    assert res.batch.rows_ok == 1, res.errors
    inv = db.scalar(select(Invoice))
    assert inv.invoice_no == "5001" and inv.customer.phone == "919800000002"


def test_missing_required_column_raises(db):
    with pytest.raises(ImportError_, match="invoice_no"):
        import_outstanding(db, "x.csv", b"Party Name,Bill Date,Amount\nA,01-10-2026,100\n")


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "919876543210"),
    ("+91 98765-43210", "919876543210"),
    ("09876543210", "919876543210"),
    (9876543210.0, "919876543210"),
    ("12345", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
