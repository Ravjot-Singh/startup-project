import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.db import SessionLocal, migrate
from app.jobs import run_tick
from app.routes import jobs, pages, promo_pages, webhooks
from app.seed import seed_rules
from app.whatsapp.provider import get_provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reminders")

LOCAL_TICK_MINUTES = 10


def local_tick() -> None:
    with SessionLocal() as db:
        run_tick(db, get_provider())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.IS_PRODUCTION and (problems := config.production_problems()):
        raise RuntimeError("Refusing to start in production:\n  - " + "\n  - ".join(problems))
    migrate()
    with SessionLocal() as db:
        seed_rules(db)
    scheduler = None
    if config.LOCAL_SCHEDULER:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        scheduler.add_job(local_tick, "interval", minutes=LOCAL_TICK_MINUTES, id="tick")
        scheduler.start()
        log.info("Local scheduler on: tick every %s minutes", LOCAL_TICK_MINUTES)
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Payment Reminder System", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=config.IS_PRODUCTION,
)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
app.include_router(jobs.router)
app.include_router(webhooks.router)
app.include_router(pages.router)
app.include_router(promo_pages.router)
