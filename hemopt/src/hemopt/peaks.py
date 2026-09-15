"""Monthly peak-power (effektavgift) tracking.

Swedish grid operators bill the average of the N highest hourly mean powers
measured on distinct days within a seasonal window. Two things follow from
that and both are implemented here:

* Only a peak that beats the current Nth highest costs anything, so the
  optimiser needs the threshold rather than the raw maximum.
* The billed quantity is an *hourly mean*, so a short burst is harmless as
  long as the rest of the clock hour compensates. The live hour accumulator
  turns that into a remaining-energy budget the controller can spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import PeakTariffConfig
from .timeutil import is_peak_window


@dataclass(frozen=True, slots=True)
class DailyPeak:
    day: datetime
    kw: float
    hour: int


@dataclass(slots=True)
class PeakState:
    """Where the current month stands against the peak tariff."""

    counted: list[DailyPeak]
    threshold_kw: float
    average_kw: float
    projected_cost_sek: float
    n_peaks: int

    @property
    def headroom_to_threshold_kw(self) -> float:
        return self.threshold_kw


def daily_maxima(hourly: dict[datetime, float], tariff: PeakTariffConfig) -> list[DailyPeak]:
    """Reduce hourly mean powers to one billable peak per day.

    `hourly` maps the local start of each clock hour to the mean power drawn
    during it. Hours outside the tariff window are dropped.
    """
    best: dict[datetime, DailyPeak] = {}
    for hour_start, kw in hourly.items():
        if not is_peak_window(hour_start, tariff.window):
            continue
        day = hour_start.replace(hour=0, minute=0, second=0, microsecond=0)
        current = best.get(day)
        if current is None or kw > current.kw:
            best[day] = DailyPeak(day=day, kw=kw, hour=hour_start.hour)
    return sorted(best.values(), key=lambda p: p.kw, reverse=True)


def peak_state(
    hourly: dict[datetime, float],
    tariff: PeakTariffConfig,
    expected_peak_kw: float,
) -> PeakState:
    """Summarise the month and derive the threshold a new peak must beat.

    Early in the month fewer than N days have been measured, so the Nth
    highest does not exist yet. Using zero there would make the optimiser
    fight peaks that will never be billed, so the fallback is
    `expected_peak_kw`: what the Nth highest is likely to settle at, seeded
    from previous months.
    """
    ranked = daily_maxima(hourly, tariff)
    counted = ranked[: tariff.n_peaks]

    if len(counted) >= tariff.n_peaks:
        threshold = counted[-1].kw
    elif counted:
        threshold = max(counted[-1].kw, expected_peak_kw)
    else:
        threshold = expected_peak_kw

    average = sum(p.kw for p in counted) / tariff.n_peaks if counted else 0.0
    return PeakState(
        counted=counted,
        threshold_kw=max(threshold, 0.0),
        average_kw=average,
        projected_cost_sek=average * tariff.effective_price_per_kw,
        n_peaks=tariff.n_peaks,
    )


@dataclass(slots=True)
class HourAccumulator:
    """Live view of the clock hour currently being measured.

    The grid meter integrates over the whole hour, so what matters is the
    energy already banked and the minutes left to spread the rest over.
    """

    hour_start: datetime
    energy_kwh: float = 0.0
    last_sample: datetime | None = None
    samples: int = 0
    _history: list[tuple[datetime, float]] = field(default_factory=list, repr=False)

    def add_sample(self, moment: datetime, power_kw: float) -> None:
        """Integrate a power reading using the interval since the last sample."""
        hour_start = moment.replace(minute=0, second=0, microsecond=0)
        if hour_start != self.hour_start:
            self.reset(hour_start)

        if self.last_sample is not None:
            elapsed_h = (moment - self.last_sample).total_seconds() / 3600.0
            if 0 < elapsed_h <= 1.0:
                previous = self._history[-1][1] if self._history else power_kw
                self.energy_kwh += 0.5 * (previous + power_kw) * elapsed_h

        self.last_sample = moment
        self.samples += 1
        self._history.append((moment, power_kw))

    def reset(self, hour_start: datetime) -> None:
        self.hour_start = hour_start
        self.energy_kwh = 0.0
        self.last_sample = None
        self.samples = 0
        self._history.clear()

    def minutes_remaining(self, now: datetime) -> float:
        end = self.hour_start + timedelta(hours=1)
        return max((end - now).total_seconds() / 60.0, 0.0)

    def projected_hour_kw(self, now: datetime, assumed_kw: float) -> float:
        """Hourly mean the meter will record if draw stays at `assumed_kw`."""
        remaining_h = self.minutes_remaining(now) / 60.0
        return self.energy_kwh + assumed_kw * remaining_h

    def allowed_kw(self, now: datetime, limit_kw: float) -> float:
        """Mean power allowed for the rest of the hour to stay under `limit_kw`.

        Returns infinity when the hour is effectively over, because a sliver of
        time cannot move the mean, and zero when the budget is already spent.
        """
        remaining_h = self.minutes_remaining(now) / 60.0
        if remaining_h <= 1.0 / 120.0:
            return float("inf")
        budget = limit_kw - self.energy_kwh
        return max(budget / remaining_h, 0.0)
