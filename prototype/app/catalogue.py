"""Products, their photos, and catalogue PDFs.

* Product photos are shrunk to JPEGs (max 1000 px) so the free database stays small.
* build_catalogue_pdf() lays out every active product (photo, name, pack, price) by category.
* Catalogues form a small library (uploaded or built PDFs); one is the "current" catalogue, which
  reorder invites send. Promotions can pick any catalogue in the library.
"""

import io
from datetime import date
from decimal import Decimal

from fpdf import FPDF
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, config, settings, storage, ui
from app.models import Campaign, Product, StoredFile

CATALOGUE = "catalogue"
PRODUCT_PHOTO = "product_photo"
OFFER_IMAGE = "offer_image"
PHOTO_MAX_PX = 1000


def _latin1(text: str) -> str:
    return (text or "").replace("₹", "Rs. ").encode("latin-1", "replace").decode("latin-1")


# ---------- photos ----------

def save_photo(db: Session, filename: str, data: bytes, kind: str = PRODUCT_PHOTO) -> StoredFile:
    """Store a photo as a JPEG no larger than PHOTO_MAX_PX (WhatsApp accepts JPEG/PNG images up to 5 MB)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise storage.StorageError(f"{filename} is not a photo (use JPG or PNG)") from None
    img = img.convert("RGB")
    img.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    stem = filename.rsplit(".", 1)[0] or "photo"
    return storage.save(db, kind, f"{stem}.jpg", "image/jpeg", out.getvalue())


# ---------- the catalogue PDF ----------

def active_products(db: Session) -> list[Product]:
    rows = db.scalars(select(Product).where(Product.is_active)).all()
    return sorted(rows, key=lambda p: ((p.category or "zzz").lower(), p.name.lower()))


def build_catalogue_pdf(db: Session, today: date | None = None) -> bytes:
    """A4 catalogue: a 3-column grid of cards (photo, category, name, pack size, price), sorted by category."""
    today = today or clock.today()
    products = active_products(db)
    if not products:
        raise storage.StorageError("Add some products first")
    margin, cols, gap = 14, 3, 6
    col_w = (210 - 2 * margin - gap * (cols - 1)) / cols
    photo_h, card_h, top, bottom = 44, 78, 34, 280
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_margins(margin, 12, margin)
    pdf.set_title(_latin1(f"{config.BUSINESS_NAME} catalogue"))

    def new_page():
        pdf.add_page()
        pdf.set_xy(margin, 12)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(31, 41, 51)
        pdf.cell(0, 9, _latin1(config.BUSINESS_NAME), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(90, 96, 105)
        pdf.cell(0, 6, _latin1(f"Product catalogue · {today.strftime('%B %Y')}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(margin, 287)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 5, _latin1("Prices may change. Reply on WhatsApp to order."))

    for i, p in enumerate(products):
        slot = i % cols
        row = (i // cols) % int((bottom - top) // card_h)
        if slot == 0 and row == 0:
            new_page()
        x, y = margin + slot * (col_w + gap), top + row * card_h
        pdf.set_draw_color(226, 224, 218)
        pdf.rect(x, y, col_w, card_h - 4)
        if p.photo_file_id and (photo := storage.load(db, p.photo_file_id)):
            img = Image.open(io.BytesIO(photo.data)).convert("RGB")
            ratio = min((col_w - 4) / img.width, (photo_h - 4) / img.height)
            w, h = img.width * ratio, img.height * ratio
            pdf.image(img, x=x + (col_w - w) / 2, y=y + 2 + (photo_h - 4 - h) / 2, w=w, h=h)
        else:
            pdf.set_fill_color(240, 239, 235)
            pdf.rect(x + 2, y + 2, col_w - 4, photo_h - 4, style="F")
        pdf.set_xy(x + 3, y + photo_h)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(14, 124, 102)
        pdf.cell(col_w - 6, 4, _latin1((p.category or "").upper())[:40], new_x="LEFT", new_y="NEXT")
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(31, 41, 51)
        pdf.multi_cell(col_w - 6, 4.6, _latin1(p.name)[:60], max_line_height=4.6, new_x="LEFT", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(90, 96, 105)
        pdf.cell(col_w - 6, 4.5, _latin1(p.unit or "")[:40], new_x="LEFT", new_y="NEXT")
        if p.price is not None:
            pdf.set_xy(x + 3, y + card_h - 11)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(14, 124, 102)
            pdf.cell(col_w - 6, 6, _latin1(f"Rs. {ui.inr(p.price)}"))
    return bytes(pdf.output())


# ---------- the catalogue library ----------

def catalogues(db: Session) -> list[StoredFile]:
    return list(db.scalars(select(StoredFile).where(StoredFile.kind == CATALOGUE).order_by(StoredFile.id.desc())).all())


def current_catalogue(db: Session) -> StoredFile | None:
    file_id = settings.get(db, "current_catalogue_file_id")
    f = storage.load(db, int(file_id)) if file_id.isdigit() else None
    return f if f is not None and f.kind == CATALOGUE else None


def add_catalogue(db: Session, filename: str, data: bytes, make_current: bool = True) -> StoredFile:
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise storage.StorageError("The catalogue must be a PDF file")
    f = storage.save(db, CATALOGUE, filename, "application/pdf", data)
    db.commit()
    if make_current:
        settings.set_value(db, "current_catalogue_file_id", f.id)
    return f


def build_and_add(db: Session, today: date | None = None) -> StoredFile:
    today = today or clock.today()
    return add_catalogue(db, f"Catalogue {today.strftime('%d %b %Y')}.pdf", build_catalogue_pdf(db, today))


def set_current(db: Session, file_id: int) -> None:
    f = storage.load(db, file_id)
    if f is None or f.kind != CATALOGUE:
        raise storage.StorageError("That catalogue no longer exists")
    settings.set_value(db, "current_catalogue_file_id", f.id)


def delete_catalogue(db: Session, file_id: int) -> None:
    f = storage.load(db, file_id)
    if f is None or f.kind != CATALOGUE:
        return
    if db.scalar(select(Campaign.id).where(Campaign.catalogue_file_id == f.id).limit(1)):
        raise storage.StorageError("A promotion uses this catalogue, so it is kept")
    if settings.get(db, "current_catalogue_file_id") == str(f.id):
        settings.set_value(db, "current_catalogue_file_id", "")
    db.delete(f)
    db.commit()


# ---------- products ----------

def save_product(db: Session, product: Product, name: str, category: str, unit: str, price: str,
                 photo: tuple[str, bytes] | None = None) -> Product:
    if not name.strip():
        raise storage.StorageError("Give the product a name")
    try:
        value = Decimal(price.replace(",", "").replace("₹", "").strip()) if price.strip() else None
    except ArithmeticError:
        raise storage.StorageError(f"“{price}” is not a price") from None
    if value is not None and value < 0:
        raise storage.StorageError("The price can't be negative")
    product.name, product.category = name.strip()[:200], (category.strip() or None)
    product.unit, product.price = (unit.strip() or None), value
    if photo and photo[1]:
        old = product.photo_file_id
        product.photo_file_id = save_photo(db, *photo).id
        if old and (f := storage.load(db, old)):
            db.delete(f)
    db.add(product)
    db.commit()
    return product
