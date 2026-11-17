import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app import config
from app.db import get_db
from app.importers.outstanding import ImportError_, import_customers, import_outstanding, normalize_phone
from app.models import (
    DISPUTED,
    UNPAID_STATUSES,
    Customer,
    ImportBatch,
    InboundMessage,
    Invoice,
    Payment,
    ReminderLog,
    ReminderRule,
)
from app.reminders.engine import call_list, run_reminders
from app.whatsapp.provider import get_provider

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
templates.env.filters["inr"] = lambda v: f"{Decimal(v or 0):,.2f}"
templates.env.globals["config"] = config

router = APIRouter()


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- auth ----------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = secrets.compare_digest(username, config.ADMIN_USERNAME) & secrets.compare_digest(password, config.ADMIN_PASSWORD)
    if not ok:
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong username or password"}, status_code=401)
    request.session["user"] = username
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---------- dashboard ----------

def _buckets(invoices: list[Invoice], today: date) -> dict[str, Decimal]:
    b = {"Not yet due": Decimal(0), "0-7 days": Decimal(0), "8-15 days": Decimal(0), "16-30 days": Decimal(0), "30+ days": Decimal(0)}
    for inv in invoices:
        d = inv.days_overdue(today)
        key = "Not yet due" if d < 0 else "0-7 days" if d <= 7 else "8-15 days" if d <= 15 else "16-30 days" if d <= 30 else "30+ days"
        b[key] += inv.balance
    return b


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    today = date.today()
    unpaid = db.scalars(select(Invoice).options(joinedload(Invoice.customer)).where(Invoice.status.in_(UNPAID_STATUSES))).all()
    total = sum((i.balance for i in unpaid), Decimal(0))
    overdue = sum((i.balance for i in unpaid if i.days_overdue(today) > 0), Decimal(0))
    per_customer: dict[int, dict] = {}
    for i in unpaid:
        r = per_customer.setdefault(i.customer_id, {"customer": i.customer, "total": Decimal(0)})
        r["total"] += i.balance
    top = sorted(per_customer.values(), key=lambda r: -r["total"])[:10]
    sent_today = db.scalar(select(func.count(ReminderLog.id)).where(ReminderLog.run_date == today, ReminderLog.status != "skipped"))
    unread = db.scalar(select(func.count(InboundMessage.id)).where(InboundMessage.handled.is_(False)))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "total": total,
            "overdue": overdue,
            "buckets": _buckets(unpaid, today),
            "top": top,
            "customers_due": len(per_customer),
            "sent_today": sent_today,
            "unread": unread,
            "summary": request.session.pop("last_run", None),
        },
    )


@router.post("/reminders/run")
def run_now(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    s = run_reminders(db, get_provider())
    request.session["last_run"] = {
        "sent": s.sent, "failed": s.failed, "skipped": s.skipped, "already_done": s.already_done, "details": s.details[:15],  # session is a cookie; keep it small
    }
    return redirect("/")


# ---------- customers ----------

@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, q: str = "", db: Session = Depends(get_db), user: str = Depends(require_login)):
    stmt = select(Customer).order_by(Customer.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.shop_name.ilike(like), Customer.phone.ilike(like), Customer.area.ilike(like)))
    rows = db.scalars(stmt).all()
    balances = dict(
        db.execute(
            select(Invoice.customer_id, func.sum(Invoice.amount - Invoice.amount_paid))
            .where(Invoice.status.in_(UNPAID_STATUSES))
            .group_by(Invoice.customer_id)
        ).all()
    )
    return templates.TemplateResponse(request, "customers.html", {"customers": rows, "balances": balances, "q": q})


@router.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    c = db.get(Customer, cid) or _404()
    invoices = db.scalars(select(Invoice).where(Invoice.customer_id == cid).order_by(Invoice.due_date.desc())).all()
    logs = db.scalars(select(ReminderLog).where(ReminderLog.customer_id == cid).order_by(ReminderLog.id.desc()).limit(20)).all()
    replies = db.scalars(select(InboundMessage).where(InboundMessage.customer_id == cid).order_by(InboundMessage.id.desc()).limit(20)).all()
    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {"c": c, "invoices": invoices, "logs": logs, "replies": replies, "today": date.today(), "error": request.query_params.get("error")},
    )


@router.post("/customers/{cid}/edit")
def customer_edit(
    cid: int,
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
            return redirect(f"/customers/{cid}?error=Invalid+phone+number")
        c.phone = normalized
    else:
        c.phone = None
    c.credit_days, c.area, c.salesman = credit_days, area or None, salesman or None
    c.whatsapp_opt_in, c.reminders_paused = whatsapp_opt_in, reminders_paused
    db.commit()
    return redirect(f"/customers/{cid}")


@router.post("/invoices/{iid}/payment")
def record_payment(
    iid: int,
    amount: str = Form(...),
    mode: str = Form("cash"),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: str = Depends(require_login),
):
    inv = db.get(Invoice, iid) or _404()
    try:
        amt = Decimal(amount.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        amt = Decimal(0)
    if amt <= 0:
        return redirect(f"/customers/{inv.customer_id}?error=Enter+a+valid+amount")
    db.add(Payment(invoice=inv, amount=amt, paid_on=date.today(), mode=mode, note=note or None))
    inv.amount_paid = Decimal(inv.amount_paid or 0) + amt
    inv.refresh_status()
    db.commit()
    return redirect(f"/customers/{inv.customer_id}")


@router.post("/invoices/{iid}/dispute")
def toggle_dispute(iid: int, db: Session = Depends(get_db), user: str = Depends(require_login)):
    inv = db.get(Invoice, iid) or _404()
    if inv.status == DISPUTED:
        inv.status = "open"
        inv.refresh_status()
    else:
        inv.status = DISPUTED
    db.commit()
    return redirect(f"/customers/{inv.customer_id}")


# ---------- import ----------

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(20)).all()
    no_phone = db.scalar(select(func.count(Customer.id)).where(Customer.phone.is_(None), Customer.is_active))
    return templates.TemplateResponse(
        request, "import.html", {"batches": batches, "result": None, "error": None, "kind": None, "no_phone": no_phone}
    )


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
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(20)).all()
    no_phone = db.scalar(select(func.count(Customer.id)).where(Customer.phone.is_(None), Customer.is_active))
    return templates.TemplateResponse(
        request, "import.html", {"batches": batches, "result": result, "error": error, "kind": kind, "no_phone": no_phone}
    )


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


# ---------- reminders, replies, call list, rules ----------

@router.get("/reminders", response_class=HTMLResponse)
def reminders(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    logs = db.scalars(
        select(ReminderLog).options(joinedload(ReminderLog.customer), joinedload(ReminderLog.rule)).order_by(ReminderLog.id.desc()).limit(200)
    ).all()
    replies = db.scalars(select(InboundMessage).options(joinedload(InboundMessage.customer)).order_by(InboundMessage.id.desc()).limit(50)).all()
    return templates.TemplateResponse(request, "reminders.html", {"logs": logs, "replies": replies})


@router.post("/replies/{mid}/handled")
def reply_handled(mid: int, db: Session = Depends(get_db), user: str = Depends(require_login)):
    m = db.get(InboundMessage, mid) or _404()
    m.handled = True
    db.commit()
    return redirect("/reminders")


@router.get("/call-list", response_class=HTMLResponse)
def call_list_page(request: Request, days: int = 15, area: str = "", db: Session = Depends(get_db), user: str = Depends(require_login)):
    rows = call_list(db, min_days_overdue=days)
    if area:
        rows = [r for r in rows if (r["customer"].area or "").lower() == area.lower()]
    areas = sorted({a for (a,) in db.execute(select(Customer.area).where(Customer.area.is_not(None)).distinct())})
    return templates.TemplateResponse(request, "call_list.html", {"rows": rows, "days": days, "area": area, "areas": areas})


@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    rows = db.scalars(select(ReminderRule).order_by(ReminderRule.offset_days)).all()
    return templates.TemplateResponse(request, "rules.html", {"rules": rows})


@router.post("/rules/{rid}/toggle")
def toggle_rule(rid: int, db: Session = Depends(get_db), user: str = Depends(require_login)):
    r = db.get(ReminderRule, rid) or _404()
    r.is_active = not r.is_active
    db.commit()
    return redirect("/rules")


def _404():
    raise HTTPException(status_code=404, detail="Not found")
