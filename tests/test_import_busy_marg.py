"""Layouts typical of Busy / Marg exports: title lines, party heading rows, totals, Dr/Cr amounts, .xls files.

These are built from the common shapes of those reports, not from real exports. When a real export
from the distributor is available, add it under sample_data/ and a test here.
"""

import io
from datetime import date, datetime
from decimal import Decimal

import xlwt
from openpyxl import Workbook
from sqlalchemy import select

from app.importers.outstanding import import_customers, import_outstanding, parse_amount
from app.models import Customer, Invoice

TODAY = date(2026, 11, 10)


def xlsx(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xls(rows) -> bytes:
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    date_style = xlwt.easyxf(num_format_str="DD-MM-YYYY")
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if v is None:
                continue
            if isinstance(v, datetime):
                ws.write(r, c, v, date_style)
            else:
                ws.write(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def invoices(db):
    return {i.invoice_no: i for i in db.scalars(select(Invoice)).all()}


# Busy-style "Bill-wise receivables": party as a heading row, bills below, party total line.
BUSY_ROWS = [
    ["ABC FROZEN FOODS"],
    ["Bill-wise Outstanding (Receivables) as on 10-11-2026"],
    [],
    ["Date", "Vch/Bill No.", "Bill Amt.", "Pending Amt.", "Due Date", "Days"],
    ["SHARMA  GENERAL STORE", None, None, None, None, None],
    [datetime(2026, 10, 1), "GST/1021", "12,500.00 Dr", "12,500.00 Dr", datetime(2026, 10, 16), 25],
    [datetime(2026, 10, 20), "GST/1077", "4,000.00 Dr", "1,500.00 Dr", datetime(2026, 11, 4), 6],
    ["Total", None, "16,500.00 Dr", "14,000.00 Dr", None, None],
    ["HOTEL PUNJAB GRILL", None, None, None, None, None],
    [datetime(2026, 9, 30), "GST/0990", "22,000.00 Dr", "22,000.00 Dr", datetime(2026, 10, 10), 31],
    [datetime(2026, 10, 25), "On Account", None, "5,000.00 Cr", None, None],
    ["Total", None, "22,000.00 Dr", "17,000.00 Dr", None, None],
    ["Grand Total", None, "38,500.00 Dr", "31,000.00 Dr", None, None],
]


def test_busy_style_grouped_xls(db):
    res = import_outstanding(db, "busy_outstanding.xls", xls(BUSY_ROWS), today=TODAY)
    assert res.errors == [] and res.batch.rows_ok == 3
    inv = invoices(db)
    assert set(inv) == {"GST/1021", "GST/1077", "GST/0990"}
    assert inv["GST/1021"].customer.name == "SHARMA GENERAL STORE"  # extra spaces collapsed
    assert inv["GST/1077"].customer is inv["GST/1021"].customer
    assert inv["GST/0990"].customer.name == "HOTEL PUNJAB GRILL"
    assert inv["GST/1077"].balance == Decimal("1500.00") and inv["GST/1077"].status == "partial"
    assert inv["GST/0990"].due_date == date(2026, 10, 10)


# Marg-style party-wise outstanding: party column filled only on the first bill, received column, no due date.
MARG_ROWS = [
    ["Party Wise Outstanding"],
    ["PARTY", "BILL NO", "DATE", "AMOUNT", "RCVD AMT", "BALANCE", "DAYS"],
    ["GUPTA KIRANA", "A-501", "26/09/2026", 6300, 0, 6300, 45],
    [None, "A-560", "15/10/2026", 2000, 500, 1500, 26],
    ["FRESH MART", "A-610", "29/10/2026", 3900, 0, 3900, 12],
    [None, None, "TOTAL", 12200, 500, 11700, None],
]


def test_marg_style_party_on_first_row_xlsx(db):
    gupta = Customer(name="Gupta Kirana", credit_days=30)  # existing customer, different case
    db.add(gupta)
    db.commit()
    res = import_outstanding(db, "marg.xlsx", xlsx(MARG_ROWS), today=TODAY)
    assert res.errors == [] and res.batch.rows_ok == 3
    inv = invoices(db)
    assert inv["A-560"].customer.id == gupta.id
    assert inv["A-560"].balance == Decimal("1500.00")
    assert inv["A-501"].due_date == date(2026, 10, 26)  # bill date + customer's 30 credit days
    assert db.scalar(select(Customer).where(Customer.name == "FRESH MART")) is not None


def test_report_with_only_pending_amount(db):
    csv = "Party Name,Bill No,Bill Date,Pending Amount\nSharma Store,B1,01-10-26,\"2,500.00 Dr\"\n"
    res = import_outstanding(db, "pending.csv", csv.encode(), today=TODAY)
    assert res.batch.rows_ok == 1
    assert invoices(db)["B1"].amount == Decimal("2500.00")


def test_customer_list_import_fills_phones(db):
    import_outstanding(db, "busy.xls", xls(BUSY_ROWS), today=TODAY)
    rows = [
        ["List of Accounts - Sundry Debtors"],
        ["Name", "Alias", "Mobile No.", "Area", "Salesman", "Credit Days"],
        ["Sharma General Store", "C001", "98765 43210, 0161-2401234", "Model Town", "Ravi", 15],
        ["Hotel Punjab Grill", "C002", "0161-2409999", "Civil Lines", "Ravi", 7],
        ["New Party", "C003", "9812345678", None, None, None],
    ]
    res = import_customers(db, "accounts.xlsx", xlsx(rows))
    assert res.batch.rows_ok == 3
    sharma = db.scalar(select(Customer).where(Customer.name == "SHARMA GENERAL STORE"))
    assert sharma.phone == "919876543210" and sharma.code == "C001" and sharma.area == "Model Town"
    hotel = db.scalar(select(Customer).where(Customer.code == "C002"))
    assert hotel.phone is None and hotel.credit_days == 7  # landline only: no WhatsApp number
    assert any("Hotel Punjab Grill" in e for e in res.errors)
    assert db.scalar(select(Customer).where(Customer.name == "New Party")).phone == "919812345678"


def test_parse_amount_dr_cr():
    assert parse_amount("12,500.00 Dr") == Decimal("12500.00")
    assert parse_amount("500.00 Cr") == Decimal("-500.00")
    assert parse_amount("(250)") == Decimal("-250.00")
    assert parse_amount("₹ 1,00,000") == Decimal("100000.00")
