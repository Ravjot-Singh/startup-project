from datetime import date, datetime
from decimal import Decimal

import pytest

from app import ui


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (5, "5"), (999, "999"), (1000, "1,000"), (12500, "12,500"), (100000, "1,00,000"),
    (1250000, "12,50,000"), (123456789, "12,34,56,789"), (Decimal("1500.50"), "1,500.50"),
    (Decimal("1500.00"), "1,500"), (Decimal("-2500"), "-2,500"), (None, "0"), ("4000", "4,000"),
])
def test_inr_indian_grouping(value, expected):
    assert ui.inr(value) == expected


def test_inr_forced_decimals():
    assert ui.inr(1500, decimals=True) == "1,500.00"


def test_when():
    today = date(2026, 11, 18)
    assert ui.when(date(2026, 11, 18), today) == "Today"
    assert ui.when(datetime(2026, 11, 17, 9, 30), today) == "Yesterday"
    assert ui.when(date(2026, 11, 19), today) == "Tomorrow"
    assert ui.when(date(2026, 11, 2), today) == "2 Nov"
    assert ui.when(date(2025, 12, 31), today) == "31 Dec 2025"
    assert ui.when(None) == ""


@pytest.mark.parametrize("offset,expected", [
    (-3, "3 days before due"), (-1, "1 day before due"), (0, "On the due date"), (1, "1 day after due"), (30, "30 days after due"),
])
def test_days_text(offset, expected):
    assert ui.days_text(offset) == expected


@pytest.mark.parametrize("days,expected", [(5, "5 days overdue"), (1, "1 day overdue"), (0, "Due today"), (-1, "Due in 1 day"), (-4, "Due in 4 days")])
def test_overdue_text(days, expected):
    assert ui.overdue_text(days) == expected


def test_when_phrase():
    today = date(2026, 11, 18)
    assert ui.when_phrase(date(2026, 11, 18), today) == "today"
    assert ui.when_phrase(date(2026, 11, 19), today, "for") == "tomorrow"
    assert ui.when_phrase(date(2026, 11, 25), today, "by") == "by 25 Nov"


def test_tone_and_status_labels():
    assert ui.tone("payment_final_notice") == "Final notice"
    assert ui.tone("some_new_template") == "Some new template"
    assert ui.status_label("dryrun") == "Test mode"
    assert ui.status_label("partial") == "Part paid"
    assert ui.status_label("weird") == "Weird"
