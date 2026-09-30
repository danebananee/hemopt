"""Is the main fuse the right size? Judged from the current on each phase.

A fuse does not care about hourly averages. It carries its rated current
indefinitely, tolerates a modest overload for a while, and blows on a
sustained or large one. The house meter reports the current on each phase
every few seconds, and Home Assistant keeps the highest and the average value
of every hour. That is enough to answer the two questions a household has:

* could we manage with a smaller fuse (a cheaper grid subscription)?
* is ours too small, and when did it come close?

The rules follow how Swedish main fuses (gG fuses or C-curve breakers)
behave: 1.13 times the rating never trips in an hour, 1.45 times trips
within an hour, and short starting currents of a heat pump or oven are
harmless. Sizes are judged against the highest hourly peak and the highest
hourly average seen on any phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Standard Swedish main fuse sizes for a house.
FUSE_SIZES = (10, 13, 16, 20, 25, 35, 50, 63)

# Enough history to say anything, and the months where the answer matters.
MIN_DAYS = 14
WINTER_MONTHS = (12, 1, 2)


@dataclass(frozen=True, slots=True)
class PhaseHour:
    hour_start: datetime
    phase: int
    max_a: float
    mean_a: float


@dataclass(slots=True)
class SizeVerdict:
    amps: int
    level: str  # comfortable | ok | tight | too_small
    peak_share: float
    sustained_share: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "amps": self.amps,
            "level": self.level,
            "peak_share": round(self.peak_share, 2),
            "sustained_share": round(self.sustained_share, 2),
        }


@dataclass(slots=True)
class FuseReport:
    configured: bool = False
    current_amps: int = 0
    days: float = 0.0
    winter_covered: bool = False
    peak_a: float = 0.0
    peak_at: datetime | None = None
    peak_phase: int | None = None
    sustained_a: float = 0.0
    verdicts: list[SizeVerdict] = field(default_factory=list)
    close_calls: list[PhaseHour] = field(default_factory=list)
    status: str = "no_data"  # no_data | learning | too_small | tight | right | can_downsize
    summary: str = ""
    detail: str = ""
    suggested_amps: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "current_amps": self.current_amps,
            "days": round(self.days, 1),
            "winter_covered": self.winter_covered,
            "peak_a": round(self.peak_a, 1),
            "peak_at": self.peak_at.isoformat() if self.peak_at else None,
            "peak_phase": self.peak_phase,
            "sustained_a": round(self.sustained_a, 1),
            "verdicts": [verdict.as_dict() for verdict in self.verdicts],
            "close_calls": [
                {
                    "hour_start": hour.hour_start.isoformat(),
                    "phase": hour.phase,
                    "max_a": round(hour.max_a, 1),
                    "mean_a": round(hour.mean_a, 1),
                }
                for hour in self.close_calls
            ],
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "suggested_amps": self.suggested_amps,
        }


def judge(amps: int, peak_a: float, sustained_a: float) -> SizeVerdict:
    """How a fuse of `amps` would have fared with this house's load."""
    peak_share = peak_a / amps
    sustained_share = sustained_a / amps
    if peak_share <= 0.8 and sustained_share <= 0.6:
        level = "comfortable"
    elif peak_share <= 1.0 and sustained_share <= 0.8:
        level = "ok"
    elif peak_share <= 1.3 and sustained_share <= 1.0:
        # Short peaks above the rating are tolerated, but there is no margin
        # for adding a car charger or a cold snap.
        level = "tight"
    else:
        level = "too_small"
    return SizeVerdict(amps, level, peak_share, sustained_share)


def assess(
    hours: list[PhaseHour],
    current_amps: float,
    *,
    extra_amps: float = 0.0,
) -> FuseReport:
    """Verdict on the current fuse and every standard size around it.

    `extra_amps` is load hemopt knows is coming but has not been measured,
    such as a newly configured car charger, added to the peak.
    """
    report = FuseReport(configured=True, current_amps=int(round(current_amps)))
    if not hours:
        report.status = "no_data"
        report.summary = "Ingen mätning av strömmen per fas än."
        report.detail = (
            "Välj elmätarens fasströmmar i Inställningar. En HomeWizard- eller "
            "Tibber-mätare på elmätarens P1-port ger dem."
        )
        return report

    moments = [hour.hour_start for hour in hours]
    report.days = (max(moments) - min(moments)).total_seconds() / 86400.0 + 1 / 24
    report.winter_covered = any(moment.month in WINTER_MONTHS for moment in moments)

    peak = max(hours, key=lambda hour: hour.max_a)
    sustained = max(hours, key=lambda hour: hour.mean_a)
    report.peak_a = peak.max_a + extra_amps
    report.peak_at = peak.hour_start
    report.peak_phase = peak.phase
    report.sustained_a = sustained.mean_a + extra_amps

    report.verdicts = [judge(size, report.peak_a, report.sustained_a) for size in FUSE_SIZES]
    by_size = {verdict.amps: verdict for verdict in report.verdicts}
    current = by_size.get(report.current_amps) or judge(
        report.current_amps, report.peak_a, report.sustained_a
    )

    threshold = 0.9 * current_amps
    calls = [hour for hour in hours if hour.max_a >= threshold]
    report.close_calls = sorted(calls, key=lambda hour: hour.hour_start, reverse=True)[:10]

    fitting = [v for v in report.verdicts if v.level in {"comfortable", "ok"}]
    smallest_ok = min((v.amps for v in fitting), default=None)
    # Advising a smaller fuse needs a margin on top of merely coping.
    roomy = [v for v in report.verdicts if v.peak_share <= 0.9 and v.sustained_share <= 0.7]
    smallest_roomy = min((v.amps for v in roomy), default=None)
    enough_data = report.days >= MIN_DAYS
    season = "" if report.winter_covered else " Mätningen täcker ingen vintermånad än."

    def times(count: int) -> str:
        return "en gång" if count == 1 else f"{count} gånger"

    if current.level == "too_small":
        report.status = "too_small"
        report.suggested_amps = smallest_ok
        report.summary = f"{report.current_amps} A är i minsta laget."
        report.detail = (
            f"Fas L{peak.phase} har dragit {report.peak_a:.0f} A och en hel timme i snitt "
            f"{report.sustained_a:.0f} A. Det kan lösa ut säkringen."
            + (f" {smallest_ok} A hade räckt med marginal." if smallest_ok else "")
            + " Sprid ut stora laster (bil, tvätt, ugn) eller låt hemopt hålla nere "
            "effekten, och prata med en elektriker."
        )
    elif current.level == "tight":
        report.status = "tight"
        report.summary = f"{report.current_amps} A räcker, men marginalen är liten."
        report.detail = (
            f"Strömmen har nått {report.peak_a:.0f} A på fas L{peak.phase}, "
            f"{times(len(calls))} över 90 % av säkringen. Korta toppar tål säkringen, "
            "men lägger du till exempelvis en laddbox behövs mer marginal."
        )
    elif smallest_roomy is not None and smallest_roomy < report.current_amps and enough_data:
        report.status = "can_downsize"
        report.suggested_amps = smallest_roomy
        report.summary = f"Du skulle klara dig med {smallest_roomy} A."
        report.detail = (
            f"Högsta ström på någon fas har varit {report.peak_a:.0f} A och högsta "
            f"timsnitt {report.sustained_a:.0f} A, med marginal under {smallest_roomy} A. "
            "En mindre säkring ger ett billigare nätabonnemang. Beställ bytet hos "
            "nätbolaget." + season
        )
    elif not enough_data:
        report.status = "learning"
        report.summary = f"hemopt har mätt strömmen i {report.days:.0f} dygn."
        report.detail = (
            f"Hittills högst {report.peak_a:.0f} A på en fas. Ett säkert råd kräver "
            f"minst {MIN_DAYS} dygn, helst med en kall period." + season
        )
    else:
        report.status = "right"
        report.summary = f"{report.current_amps} A är rätt storlek."
        report.detail = (
            f"Högst {report.peak_a:.0f} A på någon fas, god marginal till "
            f"{report.current_amps} A, och ingen mindre säkring hade räckt tryggt." + season
        )
    return report


def hours_from_samples(
    samples: dict[int, list[tuple[datetime, float]]],
) -> list[PhaseHour]:
    """Hourly max and mean per phase from hemopt's own per-minute log.

    Used when Home Assistant's statistics are not available. Minute samples
    miss the briefest spikes, which the fuse tolerates anyway.
    """
    hours: list[PhaseHour] = []
    for phase, points in samples.items():
        buckets: dict[datetime, list[float]] = {}
        for moment, amps in points:
            key = moment.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(key, []).append(abs(amps))
        for hour_start, values in buckets.items():
            if len(values) >= 10:
                hours.append(PhaseHour(hour_start, phase, max(values), sum(values) / len(values)))
    return sorted(hours, key=lambda hour: (hour.hour_start, hour.phase))
