import os
from pathlib import Path

from dotenv import load_dotenv

# The project folder (prototype/): .env and the local SQLite database live here, wherever the app is started from.
PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def normalize_db_url(url: str) -> str:
    """Neon/Render hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg (v3) driver name."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# "development" (default) or "production". Production refuses to start with default secrets.
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# Postgres (Neon) connection string. Empty or unset = a local SQLite file, prototype/reminders.db.
LOCAL_SQLITE_URL = f"sqlite:///{(PROJECT_DIR / 'reminders.db').as_posix()}"


def resolve_database_url(raw: str | None) -> str:
    return normalize_db_url((raw or "").strip().strip("'\"") or LOCAL_SQLITE_URL)


def default_local_scheduler(is_production: bool, uses_sqlite: bool) -> bool:
    return not is_production and uses_sqlite


DATABASE_URL = resolve_database_url(os.getenv("DATABASE_URL"))
USES_SQLITE = DATABASE_URL.startswith("sqlite")

# All dates/times in the app are in this zone, whatever the server clock is set to.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "ABC Frozen Foods")
BUSINESS_UPI_ID = os.getenv("BUSINESS_UPI_ID", "")

# Defaults for the settings stored in the database (the Settings page changes them later).
REMINDER_RUN_HOUR = _int("REMINDER_RUN_HOUR", 10)
SEND_WINDOW_START = _int("SEND_WINDOW_START", 9)
SEND_WINDOW_END = _int("SEND_WINDOW_END", 20)

WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "dryrun")
META_WA_TOKEN = os.getenv("META_WA_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "")
META_WA_TEMPLATE_LANG = os.getenv("META_WA_TEMPLATE_LANG", "en")
META_WA_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "change-me")
# Meta App Dashboard -> App settings -> Basic -> App secret. Used to verify webhook signatures.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")

# Secret the external cron (cron-job.org) sends in the X-Tick-Token header to POST /jobs/tick.
# Empty = the endpoint is switched off.
TICK_TOKEN = os.getenv("TICK_TOKEN", "")
# Run the tick in-process every few minutes (handy locally). Off in production, where the
# free host sleeps and the external cron does the waking. Also off by default when a local copy
# points at a shared Postgres database (e.g. the live Neon one), so it doesn't send alongside the server.
LOCAL_SCHEDULER = _bool("LOCAL_SCHEDULER", default_local_scheduler(IS_PRODUCTION, USES_SQLITE))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me")

_WEAK = {"", "change-me", "dev-secret-change-me", "change-me-to-a-long-random-string", "admin", "password"}


def production_problems() -> list[str]:
    """Reasons the current configuration is not safe to run in production (empty list = fine)."""
    problems = []
    if ADMIN_PASSWORD in _WEAK or len(ADMIN_PASSWORD) < 10:
        problems.append("ADMIN_PASSWORD must be set to a strong password (10+ characters)")
    if SESSION_SECRET in _WEAK or len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET must be a random string of 32+ characters")
    if len(TICK_TOKEN) < 24:
        problems.append("TICK_TOKEN must be a random string of 24+ characters")
    if DATABASE_URL.startswith("sqlite"):
        problems.append("DATABASE_URL must point to Postgres (files on the free host are wiped on restart)")
    if WHATSAPP_PROVIDER == "meta":
        if not META_APP_SECRET:
            problems.append("META_APP_SECRET is required to verify WhatsApp webhooks")
        if META_WA_VERIFY_TOKEN in _WEAK:
            problems.append("META_WA_VERIFY_TOKEN must be changed from the default")
    return problems
