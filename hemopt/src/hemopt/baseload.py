"""Forecast of household load the optimiser cannot control.

Cooking, laundry and everything else on the meter still counts towards the
billed hourly peak, so the planner has to leave room for it. The forecast is a
weekday-by-hour profile built from the measured total minus whatever the heat
pump was drawing at the time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median


@dataclass(slots=True)
class BaseLoadProfile:
    """Typical uncontrolled load in kW, indexed by weekday and hour."""

    grid: dict[tuple[int, int], float] = field(default_factory=dict)
    fallback_kw: float = 0.6
    samples: int = 0

    def expected_kw(self, moment: datetime) -> float:
        return self.grid.get((moment.weekday(), moment.hour), self.fallback_kw)

    def series(self, start: datetime, steps: int, step_minutes: int) -> list[float]:
        return [
            self.expected_kw(start + timedelta(minutes=step_minutes * index))
            for index in range(steps)
        ]

    def peak_hour(self) -> tuple[int, int, float] | None:
        if not self.grid:
            return None
        (weekday, hour), value = max(self.grid.items(), key=lambda item: item[1])
        return weekday, hour, value


def build_profile(
    total_power: list[tuple[datetime, float]],
    heat_pump_power: list[tuple[datetime, float]],
    fallback_kw: float,
    percentile_guard: float = 1.25,
) -> BaseLoadProfile:
    """Derive the base load profile from measured total and heat pump power.

    The median is used rather than the mean so a single laundry day does not
    inflate the forecast, then `percentile_guard` adds headroom because
    planning for exactly the median would clip the peak constraint half the
    time.
    """
    if not total_power:
        return BaseLoadProfile(fallback_kw=fallback_kw)

    pump_by_time = dict(heat_pump_power)
    pump_times = sorted(pump_by_time)

    buckets: dict[tuple[int, int], list[float]] = {}
    for moment, total in total_power:
        pump = _nearest(pump_by_time, pump_times, moment)
        base = max(total - pump, 0.0)
        buckets.setdefault((moment.weekday(), moment.hour), []).append(base)

    grid = {key: median(values) * percentile_guard for key, values in buckets.items() if values}
    return BaseLoadProfile(
        grid=grid, fallback_kw=fallback_kw, samples=sum(len(v) for v in buckets.values())
    )


def _nearest(
    values: dict[datetime, float], ordered: list[datetime], moment: datetime, tolerance_s: int = 900
) -> float:
    """Heat pump reading closest to `moment`, or zero when the log has a gap."""
    if not ordered:
        return 0.0
    best = min(ordered, key=lambda t: abs((t - moment).total_seconds()))
    if abs((best - moment).total_seconds()) > tolerance_s:
        return 0.0
    return values[best]
