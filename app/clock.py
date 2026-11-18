"""Single source of 'now' for the app, always in the business time zone (India by default).

Render and most cloud servers run on UTC; using date.today() there would make a 10:00 IST run
happen at 15:30 IST and flip 'today' at 05:30 IST. Everything goes through here instead.
Values are naive datetimes in local business time, matching what the database stores.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import config

TZ = ZoneInfo(config.TIMEZONE)


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def today() -> date:
    return now().date()
