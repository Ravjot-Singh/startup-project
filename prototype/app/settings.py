"""Owner-editable settings stored in the database, with defaults from the environment.

    settings.get_int(db, "send_window_start")
    settings.set_value(db, "sending_paused", True)

Only keys listed in DEFAULTS are allowed, so a typo fails loudly instead of silently reading a default.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app import config
from app.models import AppSetting

DEFAULTS: dict[str, Callable[[], str]] = {
    # Hour of the day (0-23, business time) from which the daily reminders go out.
    "reminder_run_hour": lambda: str(config.REMINDER_RUN_HOUR),
    # Messages are only ever sent between these hours.
    "send_window_start": lambda: str(config.SEND_WINDOW_START),
    "send_window_end": lambda: str(config.SEND_WINDOW_END),
    # Safety switch: when on, nothing is sent automatically.
    "sending_paused": lambda: "false",
    # Send each new bill's PDF on WhatsApp automatically after an upload from Busy.
    "send_bills_automatically": lambda: "true",
}


def _check(key: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")


def get(db: Session, key: str) -> str:
    _check(key)
    row = db.get(AppSetting, key)
    return row.value if row else DEFAULTS[key]()


def get_int(db: Session, key: str) -> int:
    return int(get(db, key))


def get_bool(db: Session, key: str) -> bool:
    return get(db, key).strip().lower() in ("1", "true", "yes", "on")


def set_value(db: Session, key: str, value: str | int | bool) -> None:
    _check(key)
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    row = db.get(AppSetting, key)
    if row:
        row.value = text
    else:
        db.add(AppSetting(key=key, value=text))
    db.commit()


def all_settings(db: Session) -> dict[str, str]:
    return {key: get(db, key) for key in DEFAULTS}
