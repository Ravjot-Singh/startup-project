"""Accessibility check: every page, phone and desktop size, light and dark mode, through axe-core in headless Chromium.

Fails on any 'serious' or 'critical' axe violation (e.g. low colour contrast, missing labels, unnamed buttons).
Needs the dev tools:  pip install -r requirements-dev.txt && python -m playwright install chromium
Skipped automatically when Playwright is not installed.
"""

import socket
import threading
import time
from datetime import timedelta
from decimal import Decimal

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import clock, config  # noqa: E402
from app.db import Base, get_db, init_db, make_engine  # noqa: E402
from app.models import Customer, ImportBatch, InboundMessage, Invoice, ReminderLog, ReminderRule  # noqa: E402
from app.seed import seed_rules  # noqa: E402

AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
USER, PASSWORD = "owner", "a11y-test-password"
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "phone": {"width": 375, "height": 812}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(db) -> int:
    today = clock.today()
    seed_rules(db)
    a = Customer(name="Sharma General Store", shop_name="Sharma General Store", phone="919800000001", area="Model Town", salesman="Ravi")
    b = Customer(name="Gupta Traders", shop_name="Gupta Traders", area="Civil Lines")
    db.add_all([a, b])
    db.flush()
    for no, c, days, amount, paid in [("INV1", a, 20, 12500, 0), ("INV2", a, 0, 4000, 1500), ("INV3", b, 3, 2200, 0)]:
        inv = Invoice(customer=c, invoice_no=no, invoice_date=today - timedelta(days=days + 15), due_date=today - timedelta(days=days),
                      amount=Decimal(amount), amount_paid=Decimal(paid))
        inv.refresh_status()
        db.add(inv)
    rule = db.query(ReminderRule).first()
    db.add_all([
        InboundMessage(customer=a, phone=a.phone, text="Will pay on Friday"),
        ReminderLog(customer_id=a.id, rule_id=rule.id, run_date=today, invoice_nos="INV1", amount_due=Decimal(15000),
                    message_preview="Namaste Sharma General Store, ...", status="failed", error="131026: not on WhatsApp"),
        ImportBatch(file_name="busy-outstanding.xlsx", rows_ok=3),
    ])
    db.commit()
    return a.id


@pytest.fixture(scope="module")
def server(tmp_path_factory, request):
    from app.main import app

    url = f"sqlite:///{(tmp_path_factory.mktemp('a11y') / 'a11y.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        customer_id = _seed(db)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "ADMIN_USERNAME", USER)
    mp.setattr(config, "ADMIN_PASSWORD", PASSWORD)
    app.dependency_overrides[get_db] = override
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "customer_id": customer_id}
    srv.should_exit = True
    thread.join(timeout=5)
    app.dependency_overrides.clear()
    mp.undo()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binaries not installed
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


def _axe(page) -> list[dict]:
    try:
        page.add_script_tag(url=AXE_URL)
    except Exception as e:
        pytest.skip(f"Could not load axe-core (offline?): {e}")
    result = page.evaluate("async () => await axe.run(document, {resultTypes: ['violations']})")
    return [
        {"id": v["id"], "impact": v["impact"], "help": v["help"], "nodes": [n["target"] for n in v["nodes"][:5]]}
        for v in result["violations"]
        if v["impact"] in ("serious", "critical")
    ]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", list(VIEWPORTS))
def test_pages_have_no_serious_accessibility_problems(server, browser, viewport, scheme):
    ctx = browser.new_context(viewport=VIEWPORTS[viewport], color_scheme=scheme)
    page = ctx.new_page()
    base = server["base"]
    problems = {}

    page.goto(f"{base}/login")
    if v := _axe(page):
        problems["/login"] = v
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("button:has-text('Log in')")
    page.wait_for_url(f"{base}/")

    for path in ["/", "/customers", f"/customers/{server['customer_id']}", "/call-list?days=0", "/messages",
                 "/settings", "/rules", "/import"]:
        page.goto(base + path)
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # check folded content too
        if v := _axe(page):
            problems[path] = v
    ctx.close()
    assert problems == {}, problems


def test_phone_layout_has_no_sideways_scrolling(server, browser):
    ctx = browser.new_context(viewport=VIEWPORTS["phone"])
    page = ctx.new_page()
    base = server["base"]
    page.goto(f"{base}/login")
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("button:has-text('Log in')")
    page.wait_for_url(f"{base}/")
    wide = {}
    for path in ["/", "/customers", f"/customers/{server['customer_id']}", "/call-list?days=0", "/messages", "/settings", "/import"]:
        page.goto(base + path)
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        if overflow > 1:
            wide[path] = overflow
    # the bottom tab bar is visible on phones
    page.goto(base + "/")
    assert page.locator("nav.nav").evaluate("n => getComputedStyle(n).position") == "fixed"
    ctx.close()
    assert wide == {}, f"pages wider than the phone screen (px): {wide}"
