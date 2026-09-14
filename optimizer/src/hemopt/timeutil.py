"""Time helpers, Swedish grid holidays and peak-window arithmetic."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import PeakWindow


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lunar = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lunar) // 451
    month, day = divmod(h + lunar - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=32)
def grid_holidays(year: int) -> frozenset[date]:
    """Days the tariff treats as holidays even when they fall on a weekday.

    This is the list Vattenfall Eldistribution prints on its price sheets, not
    the full set of Swedish public holidays: only days that can land Monday to
    Friday inside the winter peak window are billed as exempt.
    """
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            date(year, 1, 6),
            easter - timedelta(days=2),
            easter + timedelta(days=1),
            date(year, 12, 24),
            date(year, 12, 25),
            date(year, 12, 26),
            date(year, 12, 31),
        }
    )


def is_peak_window(moment: datetime, window: PeakWindow) -> bool:
    """True when `moment` (timezone-aware, local) is billed for peak power."""
    if moment.month not in window.months:
        return False
    if window.weekdays_only and moment.weekday() >= 5:
        return False
    if window.exclude_holidays and moment.date() in grid_holidays(moment.year):
        return False
    return window.hour_start <= moment.hour < window.hour_end


def is_high_load_energy(moment: datetime, window: PeakWindow) -> bool:
    """Whether the higher per-kWh transfer fee applies.

    Operators bill the high-load transfer rate over the same calendar as the
    peak fee, so the window definition is reused.
    """
    return is_peak_window(moment, window)


def local_tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def floor_to_step(moment: datetime, step_minutes: int) -> datetime:
    minute = (moment.minute // step_minutes) * step_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def step_range(start: datetime, steps: int, step_minutes: int) -> list[datetime]:
    return [start + timedelta(minutes=step_minutes * i) for i in range(steps)]


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month
