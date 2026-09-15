"""Hot water draw estimation and usage profiling.

Most installations have no flow meter on the hot water line, but the tank's
top sensor is enough: while the heat pump is not charging the tank, any drop
beyond the standing loss is water that was drawn. Accumulating those events
per weekday and hour gives the profile the optimiser needs to know when the
tank must be full.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import HotWaterConfig

_LOGGER = logging.getLogger(__name__)

# A drop smaller than this is sensor noise or stratification settling after a
# charge cycle, not a real tap.
MIN_DRAW_KWH = 0.05

# Typical Swedish household, used until enough history has accumulated.
DEFAULT_WEEKDAY_SHAPE = {
    6: 0.35,
    7: 0.85,
    8: 0.55,
    9: 0.2,
    12: 0.15,
    17: 0.3,
    18: 0.55,
    19: 0.7,
    20: 0.75,
    21: 0.45,
    22: 0.2,
}
DEFAULT_WEEKEND_SHAPE = {
    8: 0.4,
    9: 0.8,
    10: 0.85,
    11: 0.5,
    12: 0.3,
    17: 0.35,
    18: 0.5,
    19: 0.65,
    20: 0.7,
    21: 0.5,
    22: 0.25,
}


@dataclass(slots=True)
class TankSample:
    moment: datetime
    top_temperature: float
    charging: bool


@dataclass(frozen=True, slots=True)
class DrawEvent:
    moment: datetime
    kwh: float


@dataclass(slots=True)
class UsageProfile:
    """Expected hot water draw in kWh, indexed by weekday and hour."""

    grid: dict[tuple[int, int], float] = field(default_factory=dict)
    observations: int = 0
    fitted: bool = False

    @classmethod
    def default(cls) -> UsageProfile:
        grid: dict[tuple[int, int], float] = {}
        for weekday in range(7):
            shape = DEFAULT_WEEKEND_SHAPE if weekday >= 5 else DEFAULT_WEEKDAY_SHAPE
            for hour in range(24):
                grid[(weekday, hour)] = shape.get(hour, 0.05)
        return cls(grid=grid, observations=0, fitted=False)

    def expected_kwh(self, moment: datetime, duration_hours: float) -> float:
        """Draw expected over `duration_hours` starting at `moment`."""
        total = 0.0
        remaining = duration_hours
        cursor = moment
        while remaining > 1e-9:
            hour_end = (cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            span = min((hour_end - cursor).total_seconds() / 3600.0, remaining)
            hourly = self.grid.get((cursor.weekday(), cursor.hour), 0.05)
            total += hourly * span
            cursor += timedelta(hours=span)
            remaining -= span
        return total

    def daily_total_kwh(self) -> float:
        if not self.grid:
            return 0.0
        return sum(self.grid.values()) / 7.0

    def busiest_hours(self, weekday: int, count: int = 3) -> list[tuple[int, float]]:
        hours = [(hour, self.grid.get((weekday, hour), 0.0)) for hour in range(24)]
        hours.sort(key=lambda item: item[1], reverse=True)
        return hours[:count]


def estimate_draws(samples: list[TankSample], config: HotWaterConfig) -> list[DrawEvent]:
    """Infer tap events from tank cooling that exceeds the standing loss."""
    if len(samples) < 2:
        return []

    ordered = sorted(samples, key=lambda s: s.moment)
    kwh_per_degree = config.kwh_per_degree
    loss_per_hour = config.standing_loss_kwh_per_day / 24.0

    events: list[DrawEvent] = []
    for current, following in zip(ordered, ordered[1:], strict=False):
        elapsed_h = (following.moment - current.moment).total_seconds() / 3600.0
        if not 0 < elapsed_h <= 1.0:
            continue
        if current.charging or following.charging:
            # Mixing during a charge cycle makes the top sensor unreliable.
            continue

        drop_c = current.top_temperature - following.top_temperature
        if drop_c <= 0:
            continue

        energy_lost = drop_c * kwh_per_degree
        draw = energy_lost - loss_per_hour * elapsed_h
        if draw >= MIN_DRAW_KWH:
            events.append(DrawEvent(moment=current.moment, kwh=draw))

    return events


def build_profile(
    events: list[DrawEvent],
    days_observed: float,
    prior: UsageProfile | None = None,
    smoothing: float = 0.35,
) -> UsageProfile:
    """Blend observed draws into the previous profile.

    Exponential smoothing keeps the profile responsive to a changed routine
    without letting one unusual week dominate.
    """
    base = prior or UsageProfile.default()
    if not events or days_observed < 3:
        _LOGGER.info("hot water profile kept, %d events over %.1f days", len(events), days_observed)
        return base

    totals: dict[tuple[int, int], float] = {}
    for event in events:
        key = (event.moment.weekday(), event.moment.hour)
        totals[key] = totals.get(key, 0.0) + event.kwh

    # Each weekday recurs once a week, so the per-occurrence average divides by
    # the number of times that weekday appeared in the observation span.
    weeks = max(days_observed / 7.0, 1.0 / 7.0)
    grid = dict(base.grid)
    for weekday in range(7):
        occurrences = max(weeks, 1.0 / 7.0)
        for hour in range(24):
            observed = totals.get((weekday, hour), 0.0) / occurrences
            previous = grid.get((weekday, hour), 0.05)
            grid[(weekday, hour)] = (1.0 - smoothing) * previous + smoothing * observed

    return UsageProfile(grid=grid, observations=len(events), fitted=True)
