"""Import reports exported from the billing software (built for Busy and Marg; works with any CSV/Excel).

Two imports:
  * Outstanding report  -> invoices   ("Bill-wise Outstanding" / "Receivables" / "Party-wise Outstanding")
  * Customer list       -> customers  (party / account master with mobile numbers, area, credit days)

Busy and Marg reports differ in their column names and layout, so:
  * column headers are matched against COLUMN_ALIASES (add one here if a report uses a new header);
  * title lines above the table ("ABC Frozen Foods", "Outstanding as on ...") are skipped;
  * "grouped" reports, where the party name is a heading row with its bills listed below it
    (or appears only on the party's first bill), are handled by carrying the party name down;
  * total / sub-total rows are ignored;
  * amounts like "12,500.00 Dr" are read; a "Cr" balance means the party has paid in advance.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.models import IMPORT, UNPAID_STATUSES, Customer, ImportBatch, Invoice, Receipt, fiscal_year_of
from app.payments import apply_busy_paid, outstanding, record_fully_paid
from app.reminders.engine import queue_new_bills

COLUMN_ALIASES: dict[str, list[str]] = {
    "invoice_no": [
        "bill no", "bill number", "invoice no", "invoice number", "inv no", "vch no", "vchbill no",
        "voucher no", "voucher number", "ref no", "bill ref", "reference no", "bill", "invoice",
    ],
    "customer_name": [
        "party name", "party", "customer name", "customer", "account name", "account", "ledger name",
        "ledger", "party ac", "name", "particulars",
    ],
    "customer_code": ["party code", "customer code", "account code", "ac code", "alias", "code"],
    "invoice_date": ["bill date", "invoice date", "inv date", "vch date", "voucher date", "date", "dated"],
    "due_date": ["due date", "due dt", "due on", "due"],
    "amount": [
        "bill amount", "bill amt", "invoice amount", "inv amt", "bill value", "original amount",
        "amount", "amt", "net amount", "total amount", "grand total",
    ],
    "received": ["received", "rcvd amt", "received amt", "received amount", "paid", "paid amt", "adjusted", "adj amt"],
    "balance": [
        "pending amount", "pending amt", "pending", "balance", "balance amt", "bal amt", "balance amount",
        "outstanding", "outstanding amt", "due amount", "due amt", "balance due", "net balance", "closing balance",
    ],
    "phone": [
        "mobile", "mobile no", "mobile number", "mob no", "mob", "whatsapp", "whatsapp no",
        "phone", "phone no", "phone 1", "contact", "contact no",
    ],
    "area": ["area", "route", "beat", "city", "station", "town"],
    "salesman": ["salesman", "sales man", "salesperson", "sales person", "agent", "broker", "mr"],
    "credit_days": ["credit days", "cr days", "credit period", "due days"],
}

OUTSTANDING_REQUIRED = ("invoice_no", "invoice_date")
CUSTOMER_REQUIRED = ("customer_name",)
TOTAL_LABELS = ("total", "sub total", "subtotal", "grand total", "party total", "net total")

DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%b/%Y",
    "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%d-%b-%y", "%d %b %y",
)


class ImportError_(ValueError):
    pass


@dataclass
class ImportResult:
    batch: ImportBatch
    column_map: dict[str, str]
    errors: list[str] = field(default_factory=list)
    # Bills where payments recorded in the app are not in Busy yet (Busy shows less paid).
    busy_behind: list[str] = field(default_factory=list)
    # Bill PDFs queued to go to customers on WhatsApp (new, recent bills)
    bills_queued: int = 0


# ---------- parsing helpers ----------

def _norm(header) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", str(header or "").lower())
    return " ".join(text.split())


def clean_name(value) -> str:
    return " ".join(str(value or "").split())


def detect_columns(headers: list) -> dict[str, str]:
    """Return {field: original_header}, matching normalised headers exactly against COLUMN_ALIASES."""
    normed: dict[str, str] = {}
    for h in headers:
        n = _norm(h)
        if n and n not in normed:
            normed[n] = h
    result: dict[str, str] = {}
    used: set[str] = set()
    for fld, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            h = normed.get(alias)
            if h is not None and h not in used:
                result[fld] = h
                used.add(h)
                break
    return result


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ImportError_(f"unrecognised date '{text}'")


def parse_amount(value) -> Decimal | None:
    """'12,500.00 Dr' -> 12500.00 ; '500 Cr' -> -500.00 (credit / advance)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value).strip()
    sign = -1 if re.search(r"(?i)cr\.?$", text) else 1
    text = re.sub(r"(?i)(rs\.?|inr|₹|,|\s|dr\.?$|cr\.?$)", "", text)
    if text.startswith("(") and text.endswith(")"):
        text, sign = text[1:-1], -sign
    if text in ("", "-"):
        return None
    try:
        return (sign * Decimal(text)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ImportError_(f"unrecognised amount '{value}'")


def normalize_phone(value) -> str | None:
    """Return 91XXXXXXXXXX, or None. If a cell holds several numbers, the first mobile number wins."""
    if value is None or value == "":
        return None
    if isinstance(value, float):
        value = int(value)
    for part in re.split(r"[,/;]|\s{2,}", str(value)):
        digits = re.sub(r"\D", "", part)
        if len(digits) == 10 and digits[0] in "6789":
            return "91" + digits
        if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
            return "91" + digits[1:]
        if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
            return digits
    return None


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ---------- file reading ----------

def _raw_rows(filename: str, content: bytes) -> list[list]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        return [list(r) for r in wb.active.iter_rows(values_only=True)]
    if name.endswith(".xls"):
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=content)
        except Exception:
            raise ImportError_(
                "Could not read this .xls file. Open it in Excel and use Save As -> Excel Workbook (.xlsx) or CSV, "
                "then upload that."
            )
        sheet = book.sheet_by_index(0)
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        return rows
    text = content.decode("utf-8-sig", errors="replace")
    return [list(r) for r in csv.reader(io.StringIO(text))]


def read_table(filename: str, content: bytes, required: tuple[str, ...]) -> tuple[list[str], list[dict], dict[str, str], int]:
    """Find the header row (skipping report titles) and return (headers, rows, column_map, header_line_no)."""
    raw = _raw_rows(filename, content)
    best_idx, best_map = 0, {}
    for i, row in enumerate(raw[:30]):
        cmap = detect_columns(row)
        if all(f in cmap for f in required) and len(cmap) > len(best_map):
            best_idx, best_map = i, cmap
    if not best_map:
        first = next((r for r in raw if any(_cell_text(c) for c in r)), [])
        raise ImportError_(
            f"Could not find the column(s) {', '.join(required)} in this file "
            f"(first row: {[_cell_text(c) for c in first if _cell_text(c)]}). "
            "Check that you exported the right report, or add the header to COLUMN_ALIASES."
        )
    headers = list(raw[best_idx])
    rows = []
    for r in raw[best_idx + 1 :]:
        if any(_cell_text(c) for c in r):
            rows.append(dict(zip(headers, r)))
        else:
            rows.append(None)  # keep blank rows so reported line numbers match the file
    return headers, rows, best_map, best_idx + 1


def _getter(cmap: dict[str, str]):
    def get(row: dict, fld: str):
        col = cmap.get(fld)
        v = row.get(col) if col is not None else None
        return v.strip() if isinstance(v, str) else v

    return get


def _is_total_label(text: str) -> bool:
    n = _norm(text)
    return any(n == t or n.startswith(t + " ") for t in TOTAL_LABELS)


# ---------- customers ----------

def _find_customer(db: Session, code: str | None, name: str) -> Customer | None:
    if code:
        c = db.scalar(select(Customer).where(Customer.code == code))
        if c:
            return c
    return db.scalar(select(Customer).where(func.lower(Customer.name) == name.lower()))


def _apply_customer_fields(customer: Customer, row: dict, get) -> None:
    phone = normalize_phone(get(row, "phone"))
    if phone:
        customer.phone = phone
    for fld in ("area", "salesman"):
        v = _cell_text(get(row, fld))
        if v:
            setattr(customer, fld, v)
    days = _cell_text(get(row, "credit_days"))
    if days.isdigit():
        customer.credit_days = int(days)


def import_customers(db: Session, filename: str, content: bytes) -> ImportResult:
    """Party / account master export: fills in mobile numbers, area, salesman, credit days."""
    _, rows, cmap, header_line = read_table(filename, content, CUSTOMER_REQUIRED)
    get = _getter(cmap)
    batch = ImportBatch(file_name=f"[customers] {filename}")
    db.add(batch)
    db.flush()
    errors: list[str] = []
    for line_no, row in enumerate(rows, start=header_line + 1):
        if row is None:
            continue
        name = clean_name(get(row, "customer_name"))
        if not name or _is_total_label(name):
            continue
        code = _cell_text(get(row, "customer_code")) or None
        customer = _find_customer(db, code, name)
        if customer is None:
            customer = Customer(name=name, code=code)
            db.add(customer)
        elif code and not customer.code:
            customer.code = code
        _apply_customer_fields(customer, row, get)
        if get(row, "phone") not in (None, "") and not normalize_phone(get(row, "phone")):
            errors.append(f"Row {line_no}: {name}: '{_cell_text(get(row, 'phone'))}' is not a valid mobile number")
        db.flush()
        batch.rows_ok += 1
    batch.rows_failed = len(errors)
    batch.errors = "\n".join(errors) if errors else None
    db.commit()
    return ImportResult(batch=batch, column_map=cmap, errors=errors)


# ---------- outstanding bills ----------

def import_outstanding(
    db: Session,
    filename: str,
    content: bytes,
    full_outstanding_report: bool = True,
    today: date | None = None,
) -> ImportResult:
    """Upsert invoices from an outstanding report.

    Bills are matched on bill number + financial year (Busy restarts numbering every April).
    Paid amounts go through app.payments.apply_busy_paid, so a payment recorded in the app is
    never lowered by an upload that doesn't have it yet.

    If full_outstanding_report is True, the file is treated as the complete list of unpaid bills:
    any unpaid invoice in the database that is NOT in the file is marked paid.
    """
    today = today or clock.today()
    _, rows, cmap, header_line = read_table(filename, content, OUTSTANDING_REQUIRED)
    if "amount" not in cmap and "balance" not in cmap:
        raise ImportError_("Could not find a bill amount or pending/balance column in this file.")
    get = _getter(cmap)

    batch = ImportBatch(file_name=filename)
    db.add(batch)
    db.flush()
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()  # (bill no, financial year)
    behind: list[str] = []
    new_bills: list[Invoice] = []
    before = outstanding(db)  # to spot customers who become fully paid with this upload
    current_party: str | None = None

    for line_no, row in enumerate(rows, start=header_line + 1):
        if row is None:
            continue
        invoice_no = _cell_text(get(row, "invoice_no"))
        name = clean_name(get(row, "customer_name"))
        texts = [_cell_text(v) for v in row.values() if _cell_text(v)]

        has_amount = any(get(row, f) not in (None, "") for f in ("amount", "balance", "received"))
        if not has_amount and (not invoice_no or get(row, "invoice_date") in (None, "")):
            # A party heading. Its name may sit in any column (Busy puts it under "Date", others under "Bill No").
            first = name or clean_name(texts[0] if texts else "")
            if first and not _is_total_label(first) and not _norm(first).startswith("opening"):
                current_party = first
            continue
        if not invoice_no or _is_total_label(invoice_no):
            # Total lines, and "Opening" / "On Account" lines without a bill number, are skipped.
            continue
        if name:
            current_party = name  # party column filled only on a party's first bill
        name = name or current_party

        try:
            if not name:
                raise ImportError_("no party name for this bill")
            code = _cell_text(get(row, "customer_code")) or None
            inv_date = parse_date(get(row, "invoice_date"))
            amount = parse_amount(get(row, "amount"))
            balance = parse_amount(get(row, "balance"))
            received = parse_amount(get(row, "received"))
            if amount is None:
                amount = balance if balance is not None else None
            if inv_date is None or amount is None:
                raise ImportError_("missing bill date or amount")
            if amount < 0:
                continue  # credit note / receipt line, not a bill

            with db.begin_nested():
                customer = _find_customer(db, code, name)
                if customer is None:
                    customer = Customer(name=name, code=code)
                    db.add(customer)
                _apply_customer_fields(customer, row, get)
                db.flush()

                due = parse_date(get(row, "due_date")) or inv_date + timedelta(days=customer.credit_days or 0)
                fy = fiscal_year_of(inv_date)
                inv = db.scalar(select(Invoice).where(Invoice.invoice_no == invoice_no, Invoice.fiscal_year == fy))
                first_seen = inv is None
                if first_seen:
                    inv = Invoice(invoice_no=invoice_no, fiscal_year=fy, customer=customer, amount_paid=Decimal("0"))
                    db.add(inv)
                inv.customer = customer
                inv.invoice_date = inv_date
                inv.due_date = due
                inv.amount = amount
                inv.source_ref = f"import:{batch.id}"
                db.flush()
                busy_paid = None
                if balance is not None:  # negative ("Cr") balance = advance: treat the bill as fully paid
                    busy_paid = amount - balance
                elif received is not None:
                    busy_paid = received
                if busy_paid is not None:
                    apply_busy_paid(db, inv, busy_paid, batch, today, first_seen)
                else:
                    inv.refresh_status()
                if inv.busy_behind:
                    behind.append(invoice_no)
                if first_seen:
                    new_bills.append(inv)
            seen.add((invoice_no, fy))
            batch.rows_ok += 1
        except (ImportError_, ValueError) as e:
            batch.rows_failed += 1
            errors.append(f"Row {line_no}: {e}")

    if full_outstanding_report and batch.rows_ok > 0:
        stale = [
            inv for inv in db.scalars(select(Invoice).where(Invoice.status.in_(UNPAID_STATUSES))).all()
            if (inv.invoice_no, inv.fiscal_year) not in seen
        ]
        for inv in stale:  # gone from Busy's outstanding list = paid in full
            apply_busy_paid(db, inv, Decimal(inv.amount), batch, today, first_seen=False)
        batch.marked_paid = len(stale)

    db.flush()
    paid_in_batch = dict(db.execute(
        select(Receipt.customer_id, func.sum(Receipt.amount)).where(Receipt.import_batch_id == batch.id, Receipt.source == IMPORT)
        .group_by(Receipt.customer_id)).all())
    record_fully_paid(db, before, before.keys(), paid_in_batch)

    batch.errors = "\n".join(errors) if errors else None
    db.commit()
    if new_bills:  # a new order: a pending "time to reorder" invite is no longer needed
        from app.reminders.followups import cancel_reorders

        cancel_reorders(db, {inv.customer_id for inv in new_bills})
    queued = queue_new_bills(db, new_bills, datetime.combine(today, clock.now().time()))
    return ImportResult(batch=batch, column_map=cmap, errors=errors, busy_behind=behind, bills_queued=len(queued))
