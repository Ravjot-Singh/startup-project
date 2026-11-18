"""The periodic 'tick': everything automatic happens here.

On the free host the app sleeps when idle, so nothing can rely on an in-process timer. An external
cron (cron-job.org) calls POST /jobs/tick every ~10 minutes; that wakes the app and runs this.
Locally, LOCAL_SCHEDULER runs the same function on a timer instead.

A tick must be safe to run any number of times: each schedule step is recorded per bill once it is
queued, a customer gets at most one automatic reminder a day, and sent messages are never resent.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app import clock, settings
from app.reminders.engine import run_reminders, send_pending
from app.whatsapp.provider import WhatsAppProvider

log = logging.getLogger("reminders.tick")


def run_tick(db: Session, provider: WhatsAppProvider, now: datetime | None = None) -> dict:
    now = now or clock.now()
    result: dict = {"at": now.isoformat(timespec="minutes")}

    if settings.get_bool(db, "sending_paused"):
        result["reminders"] = "paused"
        return result

    run_hour = settings.get_int(db, "reminder_run_hour")
    if now.hour < run_hour:
        # the daily schedule hasn't started yet, but one-off reminders and retries whose time has come still go
        s = send_pending(db, provider, now)
        result["reminders"] = f"waiting until {run_hour:02d}:00"
    else:
        s = run_reminders(db, provider, today=now.date(), now=now)
        result["reminders"] = "outside send window" if s.outside_window else "ran"
    result.update(sent=s.sent, failed=s.failed, skipped=s.skipped, cancelled=s.cancelled, already_done=s.already_done)
    if s.sent or s.failed:
        log.info("Tick %s: %s", result["at"], result)
    return result
