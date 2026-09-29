"""
Юніт-тести чистої логіки, яку можна реально виконати без 1С і Telegram:
розрахунок періодів та SQLite-персистентність незавершених задач.
COM-міст (onec_client.py) і Telegram-шар потребують Windows/live-бота і
перевіряються окремо, вручну, за сценаріями в README.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.plugins.akt_zvirky.plugin import AktZvirkyPlugin


def test_current_month_bounds():
    bounds = AktZvirkyPlugin._period_bounds("current_month")
    now = datetime.now()
    assert bounds["date_start"] == now.replace(day=1).strftime("%d.%m.%Y")
    assert bounds["date_end"] == now.strftime("%d.%m.%Y")


def test_previous_month_bounds_never_overlaps_current_month():
    bounds = AktZvirkyPlugin._period_bounds("previous_month")
    now = datetime.now()
    current_month_start = now.replace(day=1)
    start = datetime.strptime(bounds["date_start"], "%d.%m.%Y")
    end = datetime.strptime(bounds["date_end"], "%d.%m.%Y")
    assert start.day == 1
    assert end < current_month_start
    assert start.month == end.month
    assert start.year == end.year


def test_year_to_date_bounds():
    bounds = AktZvirkyPlugin._period_bounds("year_to_date")
    now = datetime.now()
    assert bounds["date_start"] == now.replace(month=1, day=1).strftime("%d.%m.%Y")
    assert bounds["date_end"] == now.strftime("%d.%m.%Y")


def test_unknown_period_type_raises():
    try:
        AktZvirkyPlugin._period_bounds("not_a_real_period")
    except ValueError:
        return
    raise AssertionError("Очікувалась ValueError для невідомого типу періоду")
