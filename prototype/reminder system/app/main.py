import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.db import SessionLocal, init_db
from app.reminders.engine import run_reminders
from app.routes import pages, webhooks
from app.seed import seed_rules
from app.whatsapp.provider import get_provider

log = logging.getLogger("reminders")


def daily_job() -> None:
    with SessionLocal() as db:
        s = run_reminders(db, get_provider())
    log.info("Reminder run %s: sent=%s failed=%s skipped=%s", s.run_date, s.sent, s.failed, s.skipped)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed_rules(db)
    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_job, "cron", hour=config.REMINDER_RUN_HOUR, minute=0, id="daily_reminders")
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Payment Reminder System", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
app.include_router(webhooks.router)
app.include_router(pages.router)
