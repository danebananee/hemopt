"""The heat pump's heating curve: the lowest one that keeps every room warm.

The heat pump sets the floor-loop water temperature from the outdoor
temperature along its heating curve. The room thermostats then open and close
their loops. The curve decides two things the thermostats cannot:

* whether the coldest-to-heat room gets enough heat at all (too low a curve,
  and that room sits below its setpoint with its loop open all the time);
* how warm the water is (too high a curve, and every loop spends most of its
  time closed while the heat pump makes needlessly hot water at a worse COP).

The best curve is the lowest at which the most demanding room still reaches
its setpoint with its loop open most of the time. This module finds it from
history and turns it into a change of the heat pump's own curve settings,
which stay in the heat pump even when Home Assistant is off.

How the need is measured, per room and per band of outdoor temperature:

* `duty` is the share of time the thermostat calls for heat (LK's controller
  opens below the setpoint and closes just above, so the call follows from
  setpoint and temperature);
* the floor gives off heat in proportion to how much warmer the water is than
  the room, so the heat delivered is ``duty * (supply - room)``;
* the heat the room needs at its setpoint is what it got, scaled by the loss
  at setpoint over the loss it actually had: ``(setpoint - outdoor) /
  (room - outdoor)``.

Together that gives the supply temperature at which the room would be at its
setpoint with its loop open `TARGET_DUTY` of the time. The house needs the
highest of these over its rooms; a single room far above the rest is more
likely a loop problem than a curve problem and is reported instead.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Share of the time the most demanding room's loop should be open. Below one,
# so the thermostat still has room to react to a cold snap or an open door.
TARGET_DUTY = 0.8
# Outdoor bands for the analysis, and how much data a band needs.
BAND_C = 2.0
MIN_BAND_HOURS = 6
# Above this, the house needs too little heat for the curve to matter.
HEATING_LIMIT_C = 15.0
# Water this close to room temperature carries no heat.
MIN_LIFT_C = 2.0
# No change smaller than this is worth writing to the heat pump.
DEADBAND_C = 0.7
# A room needing this much more than the next is reported, not followed.
OUTLIER_C = 5.0
# A jump this large in the curve's own target within an hour is a change of
# the curve (by hand or by hemopt), not the weather.
CHANGE_JUMP_C = 3.0
# After a change the heat pump ramps its target for a few hours.
CHANGE_SETTLE = timedelta(hours=2)
# A curve slope outside this range (°C supply per °C outdoor) is noise from
# too little weather, not the curve.
SLOPE_RANGE = (-1.5, 0.05)
# The pump is not following its curve when, with rooms calling, the water
# stays this far below the curve's target for most hours.
NOT_FOLLOWING_C = 5.0
NOT_FOLLOWING_SHARE = 0.5
NOT_FOLLOWING_MIN_HOURS = 6
# Rule of thumb for a ground-source heat pump: each degree lower supply
# temperature saves about this share of its electricity.
COP_GAIN_PER_K = 0.025


def call_share(setpoint: float, temperature: float) -> float:
    """How open a thermostat's loop is, 0..1, from its setpoint error."""
    return max(0.0, min(1.0, (setpoint - temperature) / 0.2 + 0.5))


@dataclass(slots=True)
class RoomHour:
    temperature: float
    setpoint: float
    duty: float


@dataclass(slots=True)
class HourSample:
    moment: datetime
    outdoor: float
    supply: float | None
    target: float | None  # the curve's own supply target, if the pump reports it
    rooms: dict[str, RoomHour] = field(default_factory=dict)


@dataclass(slots=True)
class CurveRoom:
    key: str
    name: str
    priority: int


def _series(points: list[tuple[datetime, float]]) -> tuple[list[datetime], list[float]]:
    points = sorted(points)
    return [moment for moment, _ in points], [value for _, value in points]


def _at(times: list[datetime], values: list[float], moment: datetime, max_age: timedelta):
    index = bisect_right(times, moment) - 1
    if index < 0 or moment - times[index] > max_age:
        return None
    return values[index]


def build_hours(
    *,
    outdoor: list[tuple[datetime, float]],
    supply: list[tuple[datetime, float]],
    target: list[tuple[datetime, float]],
    rooms: dict[str, tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]],
    start: datetime,
    end: datetime,
) -> list[HourSample]:
    """Hourly means on a five-minute grid, holding each reading until the next.

    `rooms` maps a room key to (temperature points, setpoint points).
    """
    step = timedelta(minutes=5)
    hold = timedelta(minutes=45)
    out_s = _series(outdoor)
    sup_s = _series(supply)
    tgt_s = _series(target)
    room_s = {key: (_series(temps), _series(sets)) for key, (temps, sets) in rooms.items()}

    hours: list[HourSample] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        sums: dict[str, list[float]] = {"out": [], "sup": [], "tgt": []}
        room_sums: dict[str, list[tuple[float, float, float]]] = {key: [] for key in rooms}
        moment = hour
        while moment < hour + timedelta(hours=1):
            value = _at(*out_s, moment, timedelta(hours=2))
            if value is not None:
                sums["out"].append(value)
            value = _at(*sup_s, moment, hold)
            if value is not None:
                sums["sup"].append(value)
            value = _at(*tgt_s, moment, timedelta(hours=2))
            if value is not None:
                sums["tgt"].append(value)
            for key, (temps, sets) in room_s.items():
                temp = _at(*temps, moment, hold)
                setpoint = _at(*sets, moment, timedelta(days=2))
                if temp is not None and setpoint is not None and setpoint >= 5.0:
                    room_sums[key].append((temp, setpoint, call_share(setpoint, temp)))
            moment += step
        if len(sums["out"]) >= 6:

            def mean(values: list[float]) -> float | None:
                return sum(values) / len(values) if len(values) >= 6 else None

            def median(values: list[float]) -> float | None:
                # Hot water production sends 50 °C water past the supply
                # sensor for a few minutes; the median ignores it.
                if len(values) < 6:
                    return None
                ordered = sorted(values)
                middle = len(ordered) // 2
                if len(ordered) % 2:
                    return ordered[middle]
                return (ordered[middle - 1] + ordered[middle]) / 2

            sample = HourSample(
                moment=hour,
                outdoor=sum(sums["out"]) / len(sums["out"]),
                supply=median(sums["sup"]),
                target=mean(sums["tgt"]),
            )
            for key, values in room_sums.items():
                if len(values) >= 6:
                    sample.rooms[key] = RoomHour(
                        temperature=sum(v[0] for v in values) / len(values),
                        setpoint=sum(v[1] for v in values) / len(values),
                        duty=sum(v[2] for v in values) / len(values),
                    )
            hours.append(sample)
        hour += timedelta(hours=1)
    return hours


def _fit_line(points: list[tuple[float, float, float]]) -> tuple[float, float] | None:
    """Weighted least squares y = a + b x over (x, y, weight)."""
    weight = sum(w for _, _, w in points)
    if weight <= 0 or len(points) < 2:
        return None
    mx = sum(x * w for x, _, w in points) / weight
    my = sum(y * w for _, y, w in points) / weight
    sxx = sum(w * (x - mx) ** 2 for x, _, w in points)
    if sxx < 1e-6:
        return None
    b = sum(w * (x - mx) * (y - my) for x, y, w in points) / sxx
    return my - b * mx, b


def room_need(
    *, outdoor: float, supply: float, temperature: float, setpoint: float, duty: float
) -> float | None:
    """Supply temperature at which the room is at its setpoint with TARGET_DUTY open."""
    lift = supply - temperature
    if lift < MIN_LIFT_C or temperature - outdoor < 2.0 or setpoint - outdoor < 1.0:
        return None
    if duty < 0.03:
        # The loop hardly opens: the room is kept warm by its neighbours or
        # the sun. It sets no requirement on the curve.
        return None
    scale = (duty / TARGET_DUTY) * (setpoint - outdoor) / (temperature - outdoor)
    return setpoint + scale * lift


@dataclass(slots=True)
class CurveReport:
    status: str = "need_data"  # need_data | no_heat | too_cold | too_warm | ok
    summary: str = ""
    detail: str = ""
    hours: int = 0
    days: float = 0.0
    bands: list[dict[str, Any]] = field(default_factory=list)
    rooms: list[dict[str, Any]] = field(default_factory=list)
    current_line: tuple[float, float] | None = None
    need_line: tuple[float, float] | None = None
    outdoor_now: float | None = None
    shift_c: float = 0.0
    slope_change: float = 0.0
    savings_pct: float = 0.0
    suspects: list[str] = field(default_factory=list)
    slope_known: bool = False
    span_c: float = 0.0
    # When the curve was last changed, by hand or by hemopt, if seen.
    changed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        def line(value):
            return None if value is None else {"a": round(value[0], 3), "b": round(value[1], 4)}

        return {
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "hours": self.hours,
            "days": round(self.days, 1),
            "bands": self.bands,
            "rooms": self.rooms,
            "current_line": line(self.current_line),
            "need_line": line(self.need_line),
            "outdoor_now": None if self.outdoor_now is None else round(self.outdoor_now, 1),
            "shift_c": round(self.shift_c, 2),
            "slope_change": round(self.slope_change, 4),
            "savings_pct": round(self.savings_pct, 1),
            "suspects": self.suspects,
            "slope_known": self.slope_known,
            "span_c": round(self.span_c, 1),
        }


def curve_changed_at(hours: list[HourSample]) -> datetime | None:
    """The last hour the curve's own target jumped, i.e. the curve was changed."""
    changed = None
    previous = None
    for h in hours:
        if h.target is None:
            continue
        if previous is not None and abs(h.target - previous) >= CHANGE_JUMP_C:
            changed = h.moment
        previous = h.target
    return changed


def analyse(
    hours: list[HourSample],
    rooms: list[CurveRoom],
    *,
    now: datetime,
    since: datetime | None,
    known_slope: float | None = None,
) -> CurveReport:
    """What supply temperature the house needs, against what the curve gives.

    `since` is when hemopt last changed the curve; a change made by hand is
    found from the jump in the curve's own target. The current curve is only
    read from hours after the latest change. `known_slope` is the curve's
    slope when its two end points can be read from the heat pump.
    """
    report = CurveReport()
    names = {room.key: room.name for room in rooms}
    demanding = {room.key for room in rooms if room.priority <= 2}
    heating = [h for h in hours if h.outdoor < HEATING_LIMIT_C and h.supply is not None]
    report.hours = len(heating)
    if hours:
        report.days = (hours[-1].moment - hours[0].moment).total_seconds() / 86400
    recent = [h for h in hours if now - h.moment <= timedelta(hours=24)]
    if recent:
        report.outdoor_now = sum(h.outdoor for h in recent) / len(recent)

    if len(heating) < 24:
        report.summary = "Behöver mer mätdata."
        report.detail = (
            "Kurvan bedöms från minst ett dygn med uppvärmning (under "
            f"{HEATING_LIMIT_C:.0f} °C ute), framledningstemperatur och rummens termostater."
        )
        return report

    # The curve as it is: its own target if reported, else the measured supply.
    # Only the most recent stretch counts, so a curve changed by hand (or by
    # hemopt) is read as it is now, not blended with the old one.
    changed = curve_changed_at(hours)
    report.changed_at = changed
    if changed is not None:
        changed += CHANGE_SETTLE
        since = changed if since is None else max(since, changed)
    current_pts: list[tuple[datetime, float, float]] = []
    for h in heating:
        if since is not None and h.moment < since:
            continue
        if h.target is not None:
            current_pts.append((h.moment, h.outdoor, h.target))
            continue
        rooms_mean = _mean([r.temperature for r in h.rooms.values()])
        if h.supply - rooms_mean >= MIN_LIFT_C:
            current_pts.append((h.moment, h.outdoor, h.supply))
    report.current_line = _current_line(current_pts, now, known_slope)

    # Is the pump following its own curve at all? Rooms calling, the curve
    # asking for warm water, and the water staying cold: then the pump has
    # been told not to heat (summer mode, heat stop), and no curve helps.
    asking = [
        h
        for h in heating
        if (since is None or h.moment >= since)
        and h.target is not None
        and h.rooms
        and max(r.duty for r in h.rooms.values()) > 0.9
        and h.target > _mean([r.temperature for r in h.rooms.values()]) + MIN_LIFT_C
    ]
    short = [h for h in asking if h.target - h.supply >= NOT_FOLLOWING_C]
    if len(short) >= NOT_FOLLOWING_MIN_HOURS and len(short) >= NOT_FOLLOWING_SHARE * len(asking):
        target = _mean([h.target for h in short])
        supply = _mean([h.supply for h in short])
        report.status = "no_heat"
        report.summary = "Värmepumpen värmer inte huset."
        report.detail = (
            f"Rummen vill ha värme och värmepumpens kurva begär cirka {target:.0f} °C till "
            f"golvet, men vattnet håller bara {supply:.0f} °C ({len(short)} av "
            f"{len(asking)} timmar). Värmepumpen gör troligen bara varmvatten. Kontrollera "
            "i värmepumpens meny att värmen är på: sommarläge av, och att gränsen för "
            "värmestopp ligger över dagens utetemperatur. Kurvan ändras inte förrän "
            "värmen går igen."
        )
        return report

    # Heat delivered at all? Rooms calling but water no warmer than the rooms.
    calling = [
        h
        for h in heating
        if h.rooms and max(r.duty for r in h.rooms.values()) > 0.9 and h.outdoor < 12.0
    ]
    cold_water = [
        h for h in calling if h.supply - _mean([r.temperature for r in h.rooms.values()]) < 2.0
    ]
    if calling and len(cold_water) > 0.6 * len(calling) and len(calling) >= 12:
        report.status = "no_heat"
        report.summary = "Värmepumpen skickar inget varmt vatten till golvet."
        report.detail = (
            f"Under {len(cold_water)} timmar har rummen begärt värme medan framledningen "
            "varit nästan lika sval som rummen. Troligen står värmepumpen i sommarläge, "
            "eller så stoppar den värmen över en viss utetemperatur (värmestopp). "
            "Sänk gränsen för värmestopp eller slå på värmen i värmepumpens meny. "
            "Kurvan ändras inte förrän det är löst."
        )
        return report

    # Need per band of outdoor temperature.
    bands: dict[int, list[HourSample]] = {}
    for h in heating:
        bands.setdefault(math.floor(h.outdoor / BAND_C), []).append(h)
    need_pts = []
    room_needs: dict[str, list[tuple[float, float]]] = {}
    room_stats: dict[str, list[RoomHour]] = {}
    for _band, members in sorted(bands.items()):
        if len(members) < MIN_BAND_HOURS:
            continue
        outdoor = _mean([h.outdoor for h in members])
        supply = _mean([h.supply for h in members])
        per_room: dict[str, float] = {}
        for key in names:
            values = [h.rooms[key] for h in members if key in h.rooms]
            if len(values) < MIN_BAND_HOURS:
                continue
            room_stats.setdefault(key, []).extend(values)
            need = room_need(
                outdoor=outdoor,
                supply=supply,
                temperature=_mean([v.temperature for v in values]),
                setpoint=_mean([v.setpoint for v in values]),
                duty=_mean([v.duty for v in values]),
            )
            if need is not None:
                per_room[key] = need
                room_needs.setdefault(key, []).append((outdoor, need))
        candidates = sorted(
            ((need, key) for key, need in per_room.items() if key in demanding), reverse=True
        )
        if not candidates:
            continue
        limiting_need, limiting = candidates[0]
        if len(candidates) > 1 and limiting_need - candidates[1][0] > OUTLIER_C:
            if names[limiting] not in report.suspects:
                report.suspects.append(names[limiting])
            limiting_need, limiting = candidates[1]
        need_pts.append((outdoor, limiting_need, float(len(members))))
        report.bands.append(
            {
                "outdoor": round(outdoor, 1),
                "hours": len(members),
                "supply": round(supply, 1),
                "need": round(limiting_need, 1),
                "limiting": names[limiting],
            }
        )

    for key, name in names.items():
        stats = room_stats.get(key)
        if not stats:
            continue
        needs = room_needs.get(key, [])
        report.rooms.append(
            {
                "key": key,
                "name": name,
                "duty": round(_mean([s.duty for s in stats]), 2),
                "below": round(_mean([s.setpoint - s.temperature for s in stats]), 2),
                "need": round(_mean([n for _, n in needs]), 1) if needs else None,
                "counted": key in demanding,
            }
        )

    if not need_pts or report.current_line is None:
        report.summary = "Behöver mer mätdata."
        report.detail = "Det finns ännu för få timmar med uppvärmning i samma temperaturband."
        return report

    span = max(x for x, _, _ in need_pts) - min(x for x, _, _ in need_pts)
    cur_a, cur_b = report.current_line
    fitted = _fit_line(need_pts) if span >= 5.0 and len(need_pts) >= 3 else None
    if fitted is not None and fitted[1] <= 0.0:
        need_line = fitted
    else:
        # Too narrow a range of weather to judge the slope: keep the curve's
        # own and only move it up or down.
        weight = sum(w for _, _, w in need_pts)
        offset = sum(w * (y - (cur_a + cur_b * x)) for x, y, w in need_pts) / weight
        need_line = (cur_a + offset, cur_b)
    report.need_line = need_line

    reference = report.outdoor_now if report.outdoor_now is not None else need_pts[-1][0]
    reference = min(reference, HEATING_LIMIT_C)
    report.shift_c = (need_line[0] + need_line[1] * reference) - (cur_a + cur_b * reference)
    report.slope_change = need_line[1] - cur_b if fitted is not None else 0.0
    report.slope_known = fitted is not None
    report.span_c = span
    report.savings_pct = max(0.0, -report.shift_c) * COP_GAIN_PER_K * 100

    cold_rooms = [r["name"] for r in report.rooms if r["counted"] and r["below"] > 0.3]
    # Rooms below their setpoint with the loop open are short of heat, whatever
    # the curve's target says. Judged on the last day, room by room.
    calling_cold = []
    for key in demanding:
        values = [h.rooms[key] for h in recent if key in h.rooms]
        if len(values) >= MIN_BAND_HOURS:
            below = _mean([v.setpoint - v.temperature for v in values])
            duty = _mean([v.duty for v in values])
            if below > 0.3 and duty > 0.8:
                calling_cold.append(names[key])
    counted = [key for key in demanding if any(key in h.rooms for h in recent)]
    if report.shift_c < -DEADBAND_C and calling_cold:
        if len(calling_cold) * 2 >= max(len(counted), 1):
            report.status = "no_heat"
            report.summary = "Rummen är kalla fast kurvan är hög: värmen når inte golven."
            report.detail = (
                f"{', '.join(calling_cold)} ligger under sitt börvärde med slingan öppen, "
                "fast värmepumpens kurva begär varmare vatten än rummen behöver. Vattnet "
                "kommer alltså inte fram: troligen gör pumpen bara varmvatten (sommarläge "
                "eller värmestopp), eller så står golvvärmens cirkulationspump still. "
                "Kurvan ändras inte förrän det är löst."
            )
            return report
        report.status = "ok"
        report.summary = "Kurvan sänks inte medan rum är kalla."
        report.detail = (
            f"{', '.join(calling_cold)} ligger under sitt börvärde med slingan öppen. "
            "Övriga rum klarar sig på lägre kurva, så det kan vara en slinga som inte "
            "öppnar eller en termostat på fel rum."
        )
        return report
    if report.shift_c > DEADBAND_C:
        report.status = "too_cold"
        report.summary = (
            f"Kurvan är för låg: ungefär {report.shift_c:.1f} °C varmare vatten behövs."
        )
        report.detail = (
            (f"{', '.join(cold_rooms)} når inte sitt börvärde. " if cold_rooms else "")
            + f"Vid {reference:.0f} °C ute behövs cirka "
            f"{need_line[0] + need_line[1] * reference:.0f} °C framledning, kurvan ger "
            f"{cur_a + cur_b * reference:.0f} °C."
        )
    elif report.shift_c < -DEADBAND_C:
        report.status = "too_warm"
        report.summary = (
            f"Kurvan kan sänkas ungefär {-report.shift_c:.1f} °C utan att något rum blir kallt."
        )
        report.detail = (
            "Även det rum som behöver mest värme har sin slinga stängd en stor del av tiden. "
            f"Lägre framledning ger bättre verkningsgrad, ungefär {report.savings_pct:.0f} % "
            "mindre el till värmen."
        )
    else:
        report.status = "ok"
        report.summary = "Kurvan ligger rätt för vädret just nu."
        report.detail = (
            "Det rum som behöver mest värme har sin slinga öppen lagom stor del av tiden."
        )
    if report.suspects:
        report.detail += (
            f" {', '.join(report.suspects)} behöver mycket mer värme än övriga rum, vilket "
            "oftare beror på en felkopplad eller stängd slinga än på kurvan. Det räknas inte."
        )
    return report


def _current_line(
    points: list[tuple[datetime, float, float]],
    now: datetime,
    known_slope: float | None = None,
) -> tuple[float, float] | None:
    """The curve from the newest window that has enough weather in it.

    With the slope known from the heat pump's own settings only the level is
    read from the data. Otherwise the slope is fitted, but a slope no heating
    curve has (it comes from a day or two of nearly the same weather, or the
    pump ramping after a change) is not believed: the level is read with a
    typical floor-heating slope instead.
    """
    if not points:
        return None
    recent = [p for p in points if now - p[0] <= timedelta(hours=48)]
    if len(recent) < 6:
        recent = points[-24:]
    if known_slope is not None:
        level = sum(y - known_slope * x for _, x, y in recent) / len(recent)
        return (level, known_slope)
    for window, minimum in ((timedelta(hours=48), 12), (timedelta(days=7), 24), (None, 1)):
        chosen = [p for p in points if window is None or now - p[0] <= window]
        if len(chosen) < minimum:
            continue
        outdoors = [p[1] for p in chosen]
        if max(outdoors) - min(outdoors) < 4.0:
            continue
        line = _fit_line([(x, y, 1.0) for _, x, y in chosen])
        if line is not None and SLOPE_RANGE[0] <= line[1] <= SLOPE_RANGE[1]:
            return line
    slope = (DEFAULT_FOOT_C - DEFAULT_END_C) / 40.0
    level = sum(y - slope * x for _, x, y in recent) / len(recent)
    return (level, slope)


def _mean(values: list[float]) -> float:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else float("nan")


# --- Turning the need into settings ------------------------------------------


@dataclass(slots=True)
class Knob:
    """One of the heat pump's curve settings, as Home Assistant exposes it."""

    entity: str
    value: float
    minimum: float
    maximum: float
    step: float
    # Degrees of supply temperature per unit, measured. None until learnt.
    gain: float | None = None
    label: str = ""


@dataclass(slots=True)
class Proposal:
    entity: str
    old: float
    new: float
    expected_c: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "old": self.old,
            "new": self.new,
            "expected_c": round(self.expected_c, 2),
            "reason": self.reason,
        }


def _snap(value: float, knob: Knob, low: float, high: float) -> float:
    step = knob.step if knob.step > 0 else 0.1
    snapped = round(round(value / step) * step, 4)
    return max(max(low, knob.minimum), min(min(high, knob.maximum), snapped))


def propose(
    report: CurveReport,
    *,
    fine: Knob | None,
    curve: Knob | None,
    original: dict[str, float],
    max_daily_c: float,
    max_total_c: float,
) -> Proposal | None:
    """The next small change, or None when the curve is right or cannot move.

    The parallel shift goes to the fine adjustment when there is one; the
    curve setting itself is used for the shift otherwise, and for the slope
    once the weather has spanned enough temperatures to judge it. Gains not
    yet measured are assumed to be one degree per unit and the step is kept to
    a single unit, so the first write is also a measurement.
    """
    if report.status not in {"too_cold", "too_warm"}:
        return None
    shift = max(-max_daily_c, min(max_daily_c, report.shift_c))
    for knob, default_label in ((fine, "finjusteringen"), (curve, "kurvan")):
        if knob is None:
            continue
        label = knob.label or default_label
        gain = knob.gain if knob.gain and abs(knob.gain) > 0.05 else None
        # Until the gain is known, the first step must be large enough that its
        # effect stands out from the target's 0.1 °C resolution.
        probe = max(knob.step or 0.1, 0.5 if knob.entity.startswith("climate.") else 0.0)
        units = shift / gain if gain else math.copysign(probe, shift)
        start = original.get(knob.entity, knob.value)
        budget = max_total_c / gain if gain else 10 * (knob.step or 0.1)
        new = _snap(knob.value + units, knob, start - abs(budget), start + abs(budget))
        if abs(new - knob.value) < 1e-6:
            continue
        expected = (new - knob.value) * (gain if gain else 1.0)
        direction = "Höjer" if shift > 0 else "Sänker"
        return Proposal(
            entity=knob.entity,
            old=knob.value,
            new=new,
            expected_c=expected,
            reason=f"{direction} {label} från {knob.value:g} till {new:g}.",
        )
    return None


# A reasonable floor-heating curve before anything is known about the house:
# 20 °C to the floor at +20 °C outside, 32 °C at the design temperature.
DEFAULT_FOOT_C = 20.0
DEFAULT_END_C = 32.0
FOOT_RANGE = (16.0, 28.0)
END_RANGE = (24.0, 40.0)
# The measured slope sets the curve's shape only after this much weather.
SHAPE_SPAN_C = 8.0


@dataclass(slots=True)
class CurvePoints:
    """The heat pump's curve as two points: +20 °C outside and the design temperature."""

    foot_c: float
    end_c: float
    design_outdoor_c: float
    basis: str  # default | shifted | measured

    def at(self, outdoor: float) -> float:
        span = 20.0 - self.design_outdoor_c
        return self.foot_c + (self.end_c - self.foot_c) * (20.0 - outdoor) / span

    def as_dict(self) -> dict[str, Any]:
        return {
            "foot_c": round(self.foot_c, 1),
            "end_c": round(self.end_c, 1),
            "design_outdoor_c": self.design_outdoor_c,
            "basis": self.basis,
        }


def recommend_points(report: CurveReport, design_outdoor_c: float = -20.0) -> CurvePoints:
    """The curve's two end points that fit what the house needs.

    Before there is any data this is a typical floor-heating curve. With data
    from a narrow range of weather the typical shape is moved up or down to
    pass through what the house needs now; once the weather has spanned enough
    temperatures, the measured slope is used as well. Both points are kept
    within what floor heating sensibly runs at.
    """

    def clamp(value: float, bounds: tuple[float, float]) -> float:
        return max(bounds[0], min(bounds[1], value))

    span = 20.0 - design_outdoor_c
    default_slope = (DEFAULT_FOOT_C - DEFAULT_END_C) / span  # per °C outdoor, negative
    need = report.need_line
    if need is None or report.status in {"need_data", "no_heat"}:
        return CurvePoints(DEFAULT_FOOT_C, DEFAULT_END_C, design_outdoor_c, "default")
    a, b = need
    if not report.slope_known or report.span_c < SHAPE_SPAN_C:
        b = default_slope
        reference = report.outdoor_now if report.outdoor_now is not None else 5.0
        a = (need[0] + need[1] * reference) - b * reference
        basis = "shifted"
    else:
        basis = "measured"
    foot = clamp(a + b * 20.0, FOOT_RANGE)
    end = clamp(a + b * design_outdoor_c, END_RANGE)
    if end < foot + 4.0:
        end = foot + 4.0
    return CurvePoints(foot, end, design_outdoor_c, basis)


def learn_gain(
    *,
    before: float,
    after: float,
    outdoor_before: float,
    outdoor_after: float,
    slope: float,
    delta_units: float,
) -> float | None:
    """Degrees of supply per unit, from the curve's target before and after a write."""
    if abs(delta_units) < 1e-6:
        return None
    corrected = (after - before) - slope * (outdoor_after - outdoor_before)
    if abs(corrected) < 0.15:
        # Within the target's resolution and the pump's own drift: no measurement.
        return None
    gain = corrected / delta_units
    if not math.isfinite(gain) or abs(gain) < 0.2 or abs(gain) > 15:
        return None
    return gain
