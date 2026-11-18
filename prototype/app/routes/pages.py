import hashlib
import secrets
from datetime import date
from decimal import Decimal
from urllib.parse import quote_plus
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app import clock, config, payments, settings, ui
from app.dashboard import build_today, sending_status
from app.db import get_db
from app.importers.outstanding import ImportError_, import_customers, import_outstanding, normalize_phone
from app.models import (
    DISPUTED,
    OPENING,
    OWING_STATUSES,
    UNPAID_STATUSES,
    Customer,
    ImportBatch,
    InboundMessage,
    Invoice,
    Payment,
    Receipt,
    ReminderLog,
    ReminderRule,
)
from app.reminders.engine import call_list, run_reminders
from app.security import csrf_input, csrf_protect
from app.whatsapp.provider import get_provider

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
templates.env.filters.update(
    inr=ui.inr, when=ui.when, days_text=ui.days_text, overdue_text=ui.overdue_text, tone=ui.tone, status_label=ui.status_label
)
templates.env.globals["config"] = config
templates.env.globals["csrf_input"] = csrf_input
# Changes whenever style.css changes, so browsers fetch the new file instead of a stale cached copy.
templates.env.globals["asset_v"] = hashlib.sha256((STATIC_DIR / "style.css").read_bytes()).hexdigest()[:10]

router = APIRouter(dependencies=[Depends(csrf_protect)])

def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None, default: str) -> str:
    """Only allow redirects back into this app (no '//evil.com' or absolute URLs)."""
    return url if url and url.startswith("/") and not url.startswith("//") else default


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    base = {"today": clock.today(), "flash": request.session.pop("flash", None)}
    return templates.TemplateResponse(request, name, base | (ctx or {}), status_code=status_code)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}  # shown once on the next page


def _404():
    raise HTTPException(status_code=404, detail="Not found")


# ---------- auth ----------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = secrets.compare_digest(username, config.ADMIN_USERNAME) & secrets.compare_digest(password, config.ADMIN_PASSWORD)
    if not ok:
        return render(request, "login.html", {"error": "Wrong username or password"}, status_code=401)
    request.session["user"] = username
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---------- today ----------

@router.get("/", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return render(request, "today.html", {"v": build_today(db, clock.now())})


@router.post("/reminders/run")
def run_now(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    s = run_reminders(db, get_provider())
    if s.outside_window:
        flash(request, "Nothing sent: it's outside your sending hours. Reminders go out automatically tomorrow.", "warn")
    elif s.sent or s.failed:
        text = f"Sent {s.sent} reminder{'s' if s.sent != 1 else ''}."
        if s.failed:
            text += f" {s.failed} failed; see Needs you."
        flash(request, text, "warn" if s.failed else "ok")
    else:
        flash(request, "Nothing new to send right now. Everyone due today has already been reminded.", "ok")
    return redirect("/")


# ---------- customers ----------

@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, q: str = "", missing: str = "", db: Session = Depends(get_db), user: str = Depends(require_login)):
    stmt = select(Customer).order_by(Customer.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.shop_name.ilike(like), Customer.phone.ilike(like), Customer.area.ilike(like)))
    if missing == "phone":
        stmt = stmt.where(Customer.phone.is_(None))
    rows = db.scalars(stmt).all()
    balances = dict(
        db.execute(
            select(Invoice.customer_id, func.sum(Invoice.amount - Invoice.amount_paid))
            .where(Invoice.status.in_(UNPAID_STATUSES))
            .group_by(Invoice.customer_id)
        ).all()
    )
    if missing == "phone":
        rows = [c for c in rows if balances.get(c.id, 0) > 0]
    return render(request, "customers.html", {"customers": rows, "balances": balances, "q": q, "missing": missing})


def _customer_page(request: Request, db: Session, c: Customer, error: str | None = None,
                   values: dict | None = None, status_code: int = 200):
    invoices = db.scalars(select(Invoice).where(Invoice.customer_id == c.id).order_by(Invoice.due_date.desc())).all()
    logs = db.scalars(select(ReminderLog).where(ReminderLog.customer_id == c.id).order_by(ReminderLog.id.desc()).limit(20)).all()
    replies = db.scalars(select(InboundMessage).where(InboundMessage.customer_id == c.id).order_by(InboundMessage.id.desc()).limit(20)).all()
    receipts = db.scalars(
        select(Receipt).options(selectinload(Receipt.payments).joinedload(Payment.invoice))
        .where(Receipt.customer_id == c.id, Receipt.source != OPENING)
        .order_by(Receipt.received_on.desc(), Receipt.id.desc()).limit(20)
    ).all()
    today = clock.today()
    owing = sorted((i for i in invoices if i.status in OWING_STATUSES and i.balance > 0), key=lambda i: (i.due_date, i.id))
    unpaid = [i for i in owing if i.status in UNPAID_STATUSES]
    return render(request, "customer_detail.html", {
        "c": c, "invoices": invoices, "owing": owing, "logs": logs, "replies": replies, "receipts": receipts,
        "modes": list(payments.MODES.items()), "credit": payments.customer_credit(db, c.id),
        "due": sum((i.balance for i in unpaid), Decimal(0)),
        "overdue": sum((i.balance for i in unpaid if i.days_overdue(today) > 0), Decimal(0)),
        "error": error or request.query_params.get("error"), "values": values or {},
    }, status_code=status_code)


@router.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return _customer_page(request, db, db.get(Customer, cid) or _404())


@router.post("/customers/{cid}/edit")
def customer_edit(
    cid: int,
    request: Request,
    phone: str = Form(""),
    credit_days: int = Form(15),
    area: str = Form(""),
    salesman: str = Form(""),
    whatsapp_opt_in: bool = Form(False),
    reminders_paused: bool = Form(False),
    db: Session = Depends(get_db),
    user: str = Depends(require_login),
):
    c = db.get(Customer, cid) or _404()
    if phone.strip():
        normalized = normalize_phone(phone)
        if not normalized:
            return redirect(f"/customers/{cid}?error=That+doesn%27t+look+like+a+mobile+number")
        c.phone = normalized
    else:
        c.phone = None
    c.credit_days, c.area, c.salesman = credit_days, area or None, salesman or None
    c.whatsapp_opt_in, c.reminders_paused = whatsapp_opt_in, reminders_paused
    db.commit()
    flash(request, "Saved.")
    return redirect(f"/customers/{cid}")


def _allocations(form) -> dict[int, Decimal | None]:
    """Form fields alloc_<bill id> = amount for that bill."""
    out = {}
    for key, value in form.multi_items():
        if key.startswith("alloc_") and key[6:].isdigit() and isinstance(value, str):
            out[int(key[6:])] = payments.parse_money(value)
    return out


@router.post("/customers/{cid}/payments", response_class=HTMLResponse)
async def record_payment(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    c = db.get(Customer, cid) or _404()
    form = await request.form()
    values = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}
    try:
        amount = payments.parse_money(form.get("amount"))
        try:
            received_on = date.fromisoformat(str(form.get("received_on") or clock.today()))
        except ValueError:
            raise payments.PaymentError("Choose the date the money was received") from None
        if received_on > clock.today():
            raise payments.PaymentError("The date can't be in the future")
        receipt = payments.record_payment(db, c, amount, received_on, str(form.get("mode") or "cash"),
                                          str(form.get("note") or "").strip()[:200], _allocations(form))
    except payments.PaymentError as e:
        db.rollback()
        return _customer_page(request, db, c, error=str(e), values=values, status_code=400)
    split = receipt.allocated
    text = f"₹{ui.inr(receipt.amount)} received."
    if receipt.unallocated > 0:
        text += f" ₹{ui.inr(receipt.unallocated)} kept as credit." if split > 0 else " Kept as credit; put it against bills when you're ready."
    flash(request, text)
    return redirect(f"/customers/{cid}")


@router.post("/customers/{cid}/credit", response_class=HTMLResponse)
async def use_credit(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    c = db.get(Customer, cid) or _404()
    form = await request.form()
    try:
        used = payments.apply_credit(db, c, _allocations(form))
    except payments.PaymentError as e:
        db.rollback()
        return _customer_page(request, db, c, error=str(e), status_code=400)
    flash(request, f"₹{ui.inr(used)} of credit put against bills.")
    return redirect(f"/customers/{cid}")


@router.post("/receipts/{rid}/undo")
def undo_payment(rid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    r = db.get(Receipt, rid) or _404()
    cid, amount = r.customer_id, r.amount
    try:
        payments.undo_receipt(db, r)
    except payments.PaymentError as e:
        return redirect(f"/customers/{cid}?error=" + quote_plus(str(e)))
    flash(request, f"Payment of ₹{ui.inr(amount)} removed.")
    return redirect(f"/customers/{cid}")


@router.post("/invoices/{iid}/dispute")
def toggle_dispute(iid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    inv = db.get(Invoice, iid) or _404()
    if inv.status == DISPUTED:
        inv.status = "open"
        inv.refresh_status()
        flash(request, f"Bill {inv.invoice_no} is no longer disputed. Reminders include it again.")
    else:
        inv.status = DISPUTED
        flash(request, f"Bill {inv.invoice_no} marked disputed. It's left out of reminders.")
    db.commit()
    return redirect(f"/customers/{inv.customer_id}")


@router.get("/call-list", response_class=HTMLResponse)
def call_list_page(request: Request, days: int = 15, area: str = "", db: Session = Depends(get_db), user: str = Depends(require_login)):
    rows = call_list(db, today=clock.today(), min_days_overdue=days)
    if area:
        rows = [r for r in rows if (r["customer"].area or "").lower() == area.lower()]
    areas = sorted({a for (a,) in db.execute(select(Customer.area).where(Customer.area.is_not(None)).distinct())})
    return render(request, "call_list.html", {"rows": rows, "days": days, "area": area, "areas": areas})


# ---------- messages ----------

@router.get("/messages", response_class=HTMLResponse)
def messages(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    logs = db.scalars(
        select(ReminderLog).options(joinedload(ReminderLog.customer), joinedload(ReminderLog.rule)).order_by(ReminderLog.id.desc()).limit(200)
    ).all()
    replies = db.scalars(select(InboundMessage).options(joinedload(InboundMessage.customer)).order_by(InboundMessage.id.desc()).limit(50)).all()
    return render(request, "messages.html", {"logs": logs, "replies": replies, "status": sending_status(db)})


@router.get("/reminders")
def reminders_moved():
    return RedirectResponse("/messages", status_code=301)


@router.post("/replies/{mid}/handled")
def reply_handled(mid: int, request: Request, next: str = Form(""), db: Session = Depends(get_db), user: str = Depends(require_login)):
    m = db.get(InboundMessage, mid) or _404()
    m.handled = True
    db.commit()
    return redirect(safe_next(next, "/messages"))


# ---------- settings ----------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    rules = db.scalars(select(ReminderRule).order_by(ReminderRule.offset_days)).all()
    last_import = db.scalar(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(1))
    return render(request, "settings.html", {"status": sending_status(db), "rules": rules, "last_import": last_import, "user": user})


@router.post("/settings/pause")
def toggle_pause(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    paused = not settings.get_bool(db, "sending_paused")
    settings.set_value(db, "sending_paused", paused)
    flash(request, "Automatic sending paused. Nothing goes out until you resume." if paused
          else "Automatic sending resumed.", "warn" if paused else "ok")
    return redirect("/settings")


@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    rows = db.scalars(select(ReminderRule).order_by(ReminderRule.offset_days)).all()
    return render(request, "rules.html", {"rules": rows})


@router.post("/rules/{rid}/toggle")
def toggle_rule(rid: int, db: Session = Depends(get_db), user: str = Depends(require_login)):
    r = db.get(ReminderRule, rid) or _404()
    r.is_active = not r.is_active
    db.commit()
    return redirect("/rules")


# ---------- import ----------

def _import_ctx(db: Session) -> dict:
    return {
        "batches": db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(10)).all(),
        "no_phone": db.scalar(select(func.count(Customer.id)).where(Customer.phone.is_(None), Customer.is_active)),
    }


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return render(request, "import.html", _import_ctx(db) | {"result": None, "error": None, "kind": None})


ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xlsm", ".xls")


async def _run_import(request: Request, db: Session, file: UploadFile, fn, kind: str, **kwargs):
    error, result = None, None
    if not (file.filename or "").lower().endswith(ALLOWED_EXTENSIONS):
        error = "Please upload an Excel (.xls / .xlsx) or CSV file"
    else:
        try:
            result = fn(db, file.filename, await file.read(), **kwargs)
        except ImportError_ as e:
            db.rollback()
            error = str(e)
    return render(request, "import.html", _import_ctx(db) | {"result": result, "error": error, "kind": kind})


@router.post("/import", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    full_report: bool = Form(False),
    db: Session = Depends(get_db),
    user: str = Depends(require_login),
):
    return await _run_import(request, db, file, import_outstanding, "outstanding", full_outstanding_report=full_report)


@router.post("/import/customers", response_class=HTMLResponse)
async def import_customers_upload(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user: str = Depends(require_login)
):
    return await _run_import(request, db, file, import_customers, "customers")
