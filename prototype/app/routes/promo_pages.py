"""Promotions tab: promotions (campaigns), products, the catalogue library and leads."""

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import catalogue, clock, leads, promotions, settings, storage, ui
from app.db import get_db
from app.importers.outstanding import ImportError_
from app.models import CONTENT_CATALOGUE, DRAFT, Campaign, Customer, Product, StoredFile
from app.routes.pages import _404, flash, redirect, render, require_login
from app.security import csrf_protect

router = APIRouter(dependencies=[Depends(csrf_protect)])
VIEWABLE_KINDS = {catalogue.CATALOGUE, catalogue.PRODUCT_PHOTO, catalogue.OFFER_IMAGE}


def _choices(db: Session) -> dict:
    """Areas, salesmen and tags that exist, for the audience filters."""
    people = db.scalars(select(Customer).where(Customer.is_active)).all()
    return {
        "areas": sorted({c.area for c in people if c.area}),
        "salesmen": sorted({c.salesman for c in people if c.salesman}),
        "tags": sorted({t for c in people for t in c.tag_list}),
    }


# ---------- files (catalogues, product photos, offer photos) ----------

@router.get("/files/{fid}")
def view_file(fid: int, db: Session = Depends(get_db), user: str = Depends(require_login)):
    f = db.get(StoredFile, fid)
    if f is None or f.kind not in VIEWABLE_KINDS:
        _404()
    return Response(f.data, media_type=f.content_type, headers={"Content-Disposition": f'inline; filename="{f.filename}"'})


# ---------- the Promotions hub ----------

@router.get("/promotions", response_class=HTMLResponse)
def promotions_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.id.desc()).limit(30)).all()
    reports = {c.id: promotions.report(db, c) for c in campaigns if c.status != DRAFT}
    customers_ok = promotions.audience(db, "customers").count
    return render(request, "promotions.html", {
        "campaigns": campaigns, "reports": reports,
        "products": db.scalar(select(func.count(Product.id)).where(Product.is_active)) or 0,
        "catalogues": catalogue.catalogues(db), "current": catalogue.current_catalogue(db),
        "leads": db.scalar(select(func.count(Customer.id)).where(Customer.is_lead.is_(True))) or 0,
        "customers_ok": customers_ok,
        "customers_total": db.scalar(select(func.count(Customer.id)).where(Customer.is_lead.is_(False))) or 0,
    })


@router.get("/promotions/new", response_class=HTMLResponse)
def new_promotion(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return _new_page(request, db)


def _new_page(request: Request, db: Session, error: str | None = None, values: dict | None = None, status_code: int = 200):
    photos = [p for p in catalogue.active_products(db) if p.photo_file_id]
    return render(request, "promotion_new.html", _choices(db) | {
        "catalogues": catalogue.catalogues(db), "current": catalogue.current_catalogue(db), "photos": photos,
        "audiences": promotions.AUDIENCES, "error": error, "values": values or {},
    }, status_code=status_code)


@router.post("/promotions", response_class=HTMLResponse)
async def create_promotion(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    form = await request.form()
    values = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf_token"}
    upload = form.get("image")
    image = (upload.filename, await upload.read()) if hasattr(upload, "read") and upload.filename else None
    to_int = lambda v: int(v) if str(v or "").isdigit() else None  # noqa: E731
    try:
        c = promotions.create(
            db, values.get("name", ""), values.get("content", ""), values.get("message_line", ""), values.get("audience", ""),
            values.get("area", ""), values.get("salesman", ""), values.get("tag", ""),
            catalogue_file_id=to_int(values.get("catalogue_file_id")), image=image,
            product_photo_id=to_int(values.get("product_photo_id")),
        )
    except promotions.PromotionError as e:
        db.rollback()
        return _new_page(request, db, str(e), values, status_code=400)
    return redirect(f"/promotions/{c.id}")


def _promotion_page(request: Request, db: Session, c: Campaign, error: str | None = None, status_code: int = 200):
    from app.whatsapp.templates import CATALOGUE_PROMO_TEMPLATE, OFFER_TEMPLATE, render as render_template

    aud = promotions.campaign_audience(db, c)
    sample = aud.recipients[0].display_name if aud.recipients else "Sharma General Store"
    template = CATALOGUE_PROMO_TEMPLATE if c.content == CONTENT_CATALOGUE else OFFER_TEMPLATE
    from app import config

    preview = render_template(template, [sample, config.BUSINESS_NAME, c.message_line or "Have a look at what's new."])
    file_id = c.catalogue_file_id if c.content == CONTENT_CATALOGUE else c.image_file_id
    return render(request, "promotion.html", {
        "c": c, "aud": aud, "cost": promotions.estimate_cost(db, aud.count if c.status == DRAFT else c.queued_count),
        "rate": settings.get(db, "marketing_message_cost"), "preview": preview, "file": storage.load(db, file_id) if file_id else None,
        "report": promotions.report(db, c) if c.status != DRAFT else None, "audiences": promotions.AUDIENCES,
        "batch": settings.get_int(db, "promo_batch_size"), "error": error,
    }, status_code=status_code)


@router.get("/promotions/{cid}", response_class=HTMLResponse)
def promotion_page(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return _promotion_page(request, db, db.get(Campaign, cid) or _404())


@router.post("/promotions/{cid}/send", response_class=HTMLResponse)
def send_promotion(cid: int, request: Request, on: str = Form(""), at: str = Form(""), db: Session = Depends(get_db),
                   user: str = Depends(require_login)):
    c = db.get(Campaign, cid) or _404()
    try:
        when = None
        if on:
            try:
                when = datetime.combine(date.fromisoformat(on), time.fromisoformat(at or "11:00"))
            except ValueError:
                raise promotions.PromotionError("Choose a date and time") from None
        n = promotions.queue(db, c, when)
    except promotions.PromotionError as e:
        db.rollback()
        return _promotion_page(request, db, c, str(e), status_code=400)
    who = f"{n} {'person' if n == 1 else 'people'}"
    flash(request, f"Scheduled for {who} {ui.when_phrase(when.date(), clock.today())} at {when:%H:%M}." if when
          else f"Sending to {who}. It goes out in small batches over the next while.")
    return redirect(f"/promotions/{cid}")


@router.post("/promotions/{cid}/cancel")
def cancel_promotion(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    c = db.get(Campaign, cid) or _404()
    try:
        n = promotions.cancel(db, c)
        flash(request, f"Cancelled. {n} message{'s' if n != 1 else ''} won't be sent.", "warn")
    except promotions.PromotionError as e:
        flash(request, str(e), "warn")
    return redirect(f"/promotions/{cid}")


@router.post("/promotions/{cid}/delete")
def delete_promotion(cid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    c = db.get(Campaign, cid) or _404()
    if c.status != DRAFT:
        flash(request, "Only drafts can be deleted. Cancel it instead.", "warn")
        return redirect(f"/promotions/{cid}")
    db.delete(c)
    db.commit()
    flash(request, "Draft deleted.")
    return redirect("/promotions")


# ---------- products ----------

@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    rows = sorted(db.scalars(select(Product)).all(), key=lambda p: (not p.is_active, (p.category or "zzz").lower(), p.name.lower()))
    categories = sorted({p.category for p in rows if p.category})
    return render(request, "products.html", {"products": rows, "categories": categories})


async def _save_product_form(request: Request, db: Session, product: Product, name: str, category: str, unit: str,
                             price: str, photo: UploadFile | None):
    data = (photo.filename, await photo.read()) if photo is not None and photo.filename else None
    try:
        catalogue.save_product(db, product, name, category, unit, price, data)
        flash(request, f"Saved {product.name}.")
    except storage.StorageError as e:
        db.rollback()
        flash(request, str(e), "error")
    return redirect("/products")


@router.post("/products")
async def add_product(request: Request, name: str = Form(""), category: str = Form(""), unit: str = Form(""),
                      price: str = Form(""), photo: UploadFile | None = File(None), db: Session = Depends(get_db),
                      user: str = Depends(require_login)):
    return await _save_product_form(request, db, Product(is_active=True), name, category, unit, price, photo)


@router.post("/products/{pid}")
async def edit_product(pid: int, request: Request, name: str = Form(""), category: str = Form(""), unit: str = Form(""),
                       price: str = Form(""), photo: UploadFile | None = File(None), db: Session = Depends(get_db),
                       user: str = Depends(require_login)):
    return await _save_product_form(request, db, db.get(Product, pid) or _404(), name, category, unit, price, photo)


@router.post("/products/{pid}/toggle")
def toggle_product(pid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    p = db.get(Product, pid) or _404()
    p.is_active = not p.is_active
    db.commit()
    flash(request, f"{p.name} {'is back in' if p.is_active else 'is left out of'} the catalogue.")
    return redirect("/products")


# ---------- catalogues ----------

@router.get("/catalogues", response_class=HTMLResponse)
def catalogues_page(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    return render(request, "catalogues.html", {
        "catalogues": catalogue.catalogues(db), "current": catalogue.current_catalogue(db),
        "products": len(catalogue.active_products(db)),
    })


@router.post("/catalogues/build")
def build_catalogue(request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    try:
        f = catalogue.build_and_add(db)
        flash(request, f"Built {f.filename} from your products. Reorder invites now send it.")
    except storage.StorageError as e:
        db.rollback()
        flash(request, str(e), "error")
    return redirect("/catalogues")


@router.post("/catalogues")
async def upload_catalogue(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                           user: str = Depends(require_login)):
    try:
        f = catalogue.add_catalogue(db, file.filename or "catalogue.pdf", await file.read())
        flash(request, f"Added {f.filename}. Reorder invites now send it.")
    except storage.StorageError as e:
        db.rollback()
        flash(request, str(e), "error")
    return redirect("/catalogues")


@router.post("/catalogues/{fid}/current")
def make_current(fid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    try:
        catalogue.set_current(db, fid)
        flash(request, "Reorder invites now send this catalogue.")
    except storage.StorageError as e:
        flash(request, str(e), "error")
    return redirect("/catalogues")


@router.post("/catalogues/{fid}/delete")
def delete_catalogue(fid: int, request: Request, db: Session = Depends(get_db), user: str = Depends(require_login)):
    try:
        catalogue.delete_catalogue(db, fid)
        flash(request, "Catalogue removed.")
    except storage.StorageError as e:
        flash(request, str(e), "warn")
    return redirect("/catalogues")


# ---------- leads ----------

@router.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, q: str = "", db: Session = Depends(get_db), user: str = Depends(require_login)):
    stmt = select(Customer).where(Customer.is_lead.is_(True)).order_by(Customer.name)
    rows = db.scalars(stmt).all()
    if q:
        ql = q.lower()
        rows = [c for c in rows if ql in c.name.lower() or ql in (c.phone or "") or ql in (c.area or "").lower() or ql in (c.tags or "")]
    return render(request, "leads.html", {"leads": rows, "q": q, "result": None, "error": None})


@router.post("/leads")
def add_lead(request: Request, name: str = Form(""), phone: str = Form(""), area: str = Form(""), tags: str = Form(""),
             consent_source: str = Form(""), db: Session = Depends(get_db), user: str = Depends(require_login)):
    try:
        person, created = leads.add_or_update(db, name, phone, consent_source, area, tags)
        db.commit()
    except leads.LeadError as e:
        db.rollback()
        flash(request, str(e), "error")
        return redirect("/leads")
    flash(request, f"Added {person.display_name}." if created
          else f"{person.display_name} was already here (same number): tags and consent updated.")
    return redirect("/leads")


@router.post("/leads/import", response_class=HTMLResponse)
async def import_leads(request: Request, file: UploadFile = File(...), consent_source: str = Form(""),
                       db: Session = Depends(get_db), user: str = Depends(require_login)):
    error, result = None, None
    if not (file.filename or "").lower().endswith((".csv", ".xlsx", ".xlsm", ".xls")):
        error = "Please upload an Excel (.xls / .xlsx) or CSV file"
    else:
        try:
            result = leads.import_leads(db, file.filename, await file.read(), consent_source)
        except ImportError_ as e:
            db.rollback()
            error = str(e)
    rows = db.scalars(select(Customer).where(Customer.is_lead.is_(True)).order_by(Customer.name)).all()
    return render(request, "leads.html", {"leads": rows, "q": "", "result": result, "error": error})


# ---------- settings for promotions ----------

@router.post("/settings/promotions")
def save_promotion_settings(request: Request, cost: str = Form(""), batch: int = Form(40), db: Session = Depends(get_db),
                            user: str = Depends(require_login)):
    from decimal import Decimal, InvalidOperation

    try:
        rate = Decimal(cost.replace("₹", "").strip())
    except InvalidOperation:
        rate = Decimal(-1)
    if not (Decimal(0) <= rate <= Decimal(20)) or not (1 <= batch <= 500):
        flash(request, "Enter a cost between 0 and 20 rupees, and a batch size between 1 and 500.", "error")
        return redirect("/settings#promo")
    settings.set_value(db, "marketing_message_cost", f"{rate:.2f}")
    settings.set_value(db, "promo_batch_size", batch)
    flash(request, "Saved.")
    return redirect("/settings#promo")
