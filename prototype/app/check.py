"""Check a configuration before (or after) going live:  python -m app.check

Reads the same settings the app uses (.env locally, the Environment tab on Render) and checks them. It
only reads: it doesn't create tables, change data or send messages. Secrets are never printed.

Exit code 0 = ready, 1 = something must be fixed first.
"""

import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from sqlalchemy import inspect, text

from app import config
from app.whatsapp.provider import MetaCloudProvider

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}


@dataclass
class Report:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text_: str) -> None:
        self.lines.append((level, text_))

    @property
    def failed(self) -> bool:
        return any(level == FAIL for level, _ in self.lines)


def database_label(url: str) -> str:
    """Where the database is, without the password."""
    if url.startswith("sqlite"):
        return "a local SQLite file (prototype/reminders.db)" if url == config.LOCAL_SQLITE_URL else "a SQLite file"
    parts = urlsplit(url)
    return f"Postgres at {parts.hostname or '?'} (database {parts.path.lstrip('/') or '?'})"


def check_database(report: Report, url: str) -> None:
    from alembic.script import ScriptDirectory

    from app.db import alembic_config, make_engine

    report.add(OK if not url.startswith("sqlite") or not config.IS_PRODUCTION else FAIL,
               f"Database: {database_label(url)}")
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            if engine.dialect.name == "postgresql":
                version = conn.scalar(text("SHOW server_version"))
                report.add(OK, f"Connected (Postgres {version.split()[0]})")
            else:
                report.add(OK, "Connected")
            head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
            current = None
            if inspect(conn).has_table("alembic_version"):
                current = conn.scalar(text("SELECT version_num FROM alembic_version"))
            if current == head:
                report.add(OK, f"Tables are up to date (version {head})")
            elif current is None:
                report.add(WARN, "The database is empty: the app creates the tables when it first starts")
            else:
                report.add(WARN, f"Tables are at version {current}; the app upgrades them to {head} when it starts")
    except Exception as e:  # any driver error: bad password, host, SSL ...
        reason = str(e).strip().splitlines()[0][:200]
        report.add(FAIL, f"Can't connect to the database: {reason}")
    finally:
        engine.dispose()


def check_whatsapp(report: Report, client: httpx.Client | None = None) -> None:
    if config.WHATSAPP_PROVIDER != "meta":
        report.add(OK, "WhatsApp: test mode (WHATSAPP_PROVIDER=dryrun): messages are listed, nothing is sent")
        return
    missing = [n for n in ("META_WA_TOKEN", "META_WA_PHONE_NUMBER_ID", "META_APP_SECRET") if not getattr(config, n)]
    if missing:
        report.add(FAIL, f"WhatsApp is switched on but these are empty: {', '.join(missing)}")
        return
    url = f"https://graph.facebook.com/{MetaCloudProvider.API_VERSION}/{config.META_WA_PHONE_NUMBER_ID}"
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.get(url, params={"fields": "display_phone_number,verified_name,quality_rating"},
                          headers={"Authorization": f"Bearer {config.META_WA_TOKEN}"})
    except httpx.HTTPError as e:
        report.add(FAIL, f"Can't reach WhatsApp (Meta): {e}")
        return
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {}).get("message", "")
        except ValueError:
            err = resp.text[:200]
        report.add(FAIL, f"Meta rejected the token or phone number ID: {err}")
        return
    info = resp.json()
    report.add(OK, f"WhatsApp: connected as {info.get('verified_name', '?')} ({info.get('display_phone_number', '?')}),"
                   f" quality {info.get('quality_rating', 'unknown')}")


def run(client: httpx.Client | None = None) -> Report:
    report = Report()
    report.add(OK, f"Mode: {config.APP_ENV}")
    if config.IS_PRODUCTION:
        for problem in config.production_problems():
            report.add(FAIL, problem)
    elif config.ADMIN_PASSWORD in ("change-me", "admin", "password"):
        report.add(WARN, "ADMIN_PASSWORD is still the example value")
    check_database(report, config.DATABASE_URL)
    if not config.DATABASE_URL.startswith("sqlite") and not config.IS_PRODUCTION:
        report.add(WARN, "This computer uses the shared Postgres database: changes here change the live data."
                         + ("" if config.LOCAL_SCHEDULER else " Automatic sending from this computer is off."))
    if config.LOCAL_SCHEDULER:
        report.add(OK, "Automatic sending: built-in timer every 10 minutes (LOCAL_SCHEDULER)")
    elif config.TICK_TOKEN:
        report.add(OK, "Automatic sending: waits for cron-job.org to call /jobs/tick (TICK_TOKEN is set)")
    else:
        report.add(WARN if not config.IS_PRODUCTION else FAIL,
                   "Automatic sending is off: set TICK_TOKEN (server) or LOCAL_SCHEDULER=true (this computer)")
    if config.BUSINESS_NAME == "ABC Frozen Foods":
        report.add(WARN, "BUSINESS_NAME is still the example name")
    if not config.BUSINESS_UPI_ID or config.BUSINESS_UPI_ID == "abcfrozen@okaxis":
        report.add(WARN, "BUSINESS_UPI_ID is empty or the example: bills will have no working UPI link or QR")
    check_whatsapp(report, client)
    return report


def main() -> int:
    report = run()
    for level, line in report.lines:
        print(f"{MARK[level]} {line}")
    print()
    print("Fix the [FAIL] lines before going live." if report.failed else "Ready.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
