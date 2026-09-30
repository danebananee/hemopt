"""Shadow accounting: what hemopt and a quarter-hour contract would have saved.

While the household is still on a daily or monthly average price, moving load
in time saves nothing, so the optimiser cannot prove its worth on the bill.
This module keeps a running ledger that answers the question anyway.

Two model houses are run side by side, one planning step at a time:

* the **reference twin** is heated the way the house is heated without
  hemopt: each loop opens as far as its thermostat's setpoint error says,
  and the heat pump keeps the tank topped up as water is drawn;
* the **optimised twin** follows hemopt's plan.

The real house is always one of them, measured afresh every step, so model
error cannot build up over weeks. While hemopt only watches, the real house is
the reference twin; once it steers, the real house is the optimised twin. What
is carried from step to step is the *difference* between them: how much
warmer the optimised twin's rooms and floors are, and how much hotter its
tank. That difference is the heat hemopt has banked or borrowed. It has to be
paid back later, so load that was moved is never counted as load that
disappeared. Both twins use the same room models and the same heat pump
model, so errors in those scale both sides alike instead of showing up as a
saving.

Each step books the heat pump energy each twin draws. Together with the
measured house consumption this splits the saving in two:

* **contract effect** — the same consumption, billed per quarter hour instead
  of at the day's (or month's) average;
* **control effect** — hemopt moving the heat pump's consumption to cheaper
  quarters, billed per quarter hour.

The peak tariff is not part of the ledger: it is billed on the whole house
over a month and does not depend on the retail contract.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from .config import CONTRACT_NAMES, Config, Contract, EnergyPriceConfig

if TYPE_CHECKING:  # the optimiser pulls in HiGHS; storage only needs LedgerStep
    from .optimizer import OptimisationInput, Plan, RoomInput

# The twins are allowed to drift apart by this much at most. The difference is
# banked heat; anything larger means the model and the house disagree, and the
# ledger should not keep compounding it.
MAX_ROOM_OFFSET_K = 3.0
MAX_TANK_OFFSET_K = 15.0


@dataclass(slots=True)
class TwinOffset:
    """Optimised twin minus reference twin, carried between steps."""

    rooms: dict[str, tuple[float, float]] = field(default_factory=dict)
    tank_c: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rooms": {key: [temp, slab] for key, (temp, slab) in self.rooms.items()},
            "tank_c": self.tank_c,
        }

    @classmethod
    def from_dict(cls, payload: object) -> TwinOffset:
        if not isinstance(payload, dict):
            return cls()
        rooms: dict[str, tuple[float, float]] = {}
        for key, value in (payload.get("rooms") or {}).items():
            try:
                temp, slab = float(value[0]), float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(temp) and math.isfinite(slab):
                rooms[str(key)] = (temp, slab)
        try:
            tank = float(payload.get("tank_c", 0.0))
        except (TypeError, ValueError):
            tank = 0.0
        return cls(rooms=rooms, tank_c=tank if math.isfinite(tank) else 0.0)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def shifted_problem(
    problem: OptimisationInput, offset: TwinOffset, sign: float
) -> OptimisationInput:
    """The same planning problem, started from the real house plus `sign * offset`."""
    rooms: list[RoomInput] = []
    for room in problem.rooms:
        temp_offset, slab_offset = offset.rooms.get(room.config.key, (0.0, 0.0))
        slab = (
            room.slab_start(problem.outdoor_c[0], problem.sun_at(0), problem.wind_at(0))
            + sign * slab_offset
        )
        rooms.append(
            replace(
                room,
                initial_temperature=room.initial_temperature + sign * temp_offset,
                initial_slab=max(0.0, min(1.0, slab)),
            )
        )
    hot_water = problem.hot_water
    if hot_water is not None and offset.tank_c:
        hot_water = replace(
            hot_water, initial_temperature=hot_water.initial_temperature + sign * offset.tank_c
        )
    return replace(problem, rooms=rooms, hot_water=hot_water)


def call_for_heat(setpoint: float, indoor: float) -> float:
    """Loop opening implied by a thermostat's setpoint error, 0..1.

    The same proxy the room models are trained on, so the reference twin is
    driven by exactly the signal the models understand.
    """
    return max(0.0, min(1.0, (setpoint - indoor) / 0.5))


@dataclass(slots=True)
class ReferenceStep:
    """The reference twin after one step of thermostat-only heating."""

    rooms: dict[str, tuple[float, float]]
    tank_c: float | None
    electrical_kwh: float
    cold_degree_hours: float


def reference_step(
    problem: OptimisationInput,
    setpoints: dict[str, float | None],
    start_offset: TwinOffset | None = None,
) -> ReferenceStep:
    """Advance the reference twin one step from the real house (minus an offset).

    `setpoints` holds each room's thermostat setpoint. A room without one is
    assumed to be held where it is, which is what a working thermostat does.
    """
    dt = problem.step_hours
    outdoor = problem.outdoor_c[0]
    heat_pump = problem.heat_pump
    offset = start_offset or TwinOffset()

    thermal_kw = 0.0
    cold = 0.0
    rooms: dict[str, tuple[float, float]] = {}
    for room in problem.rooms:
        key = room.config.key
        temp_offset, slab_offset = offset.rooms.get(key, (0.0, 0.0))
        indoor = room.initial_temperature - temp_offset
        sun, wind = problem.sun_at(0), problem.wind_at(0)
        slab = max(0.0, min(1.0, room.slab_start(outdoor, sun, wind) - slab_offset))
        setpoint = setpoints.get(key)
        loop = (
            call_for_heat(setpoint, indoor)
            if setpoint is not None
            else room.model.steady_heat_fraction(indoor, outdoor, sun=sun, wind_ms=wind)
        )
        indoor_next, slab_next = room.model.advance(
            indoor, slab, outdoor, loop, dt, sun=sun, wind_ms=wind
        )
        rooms[key] = (indoor_next, slab_next)
        thermal_kw += loop * room.nominal_heat_kw
        cold += max(room.config.comfort_min - indoor_next, 0.0) * dt

    thermal_kw = min(thermal_kw, heat_pump.max_thermal_kw)
    electrical = thermal_kw * dt / max(heat_pump.cop(outdoor, hot_water=False), 1e-6)

    tank: float | None = None
    hot_water = problem.hot_water
    if hot_water is not None and hot_water.config.enabled:
        # The heat pump's own tank control replaces what is drawn and lost.
        loss = hot_water.config.standing_loss_kwh_per_day / 24.0 * dt
        replaced = hot_water.draw_kwh[0] + loss
        electrical += replaced / max(heat_pump.cop(outdoor, hot_water=True), 1e-6)
        tank = hot_water.initial_temperature - offset.tank_c

    return ReferenceStep(
        rooms=rooms, tank_c=tank, electrical_kwh=electrical, cold_degree_hours=cold
    )


def next_offset(optimised: Plan, reference: ReferenceStep) -> TwinOffset:
    """How far apart the twins are after this step."""
    rooms: dict[str, tuple[float, float]] = {}
    for room in optimised.rooms:
        other = reference.rooms.get(room.key)
        if other is None or not room.temperature or not room.slab:
            continue
        temp = _clamp(room.temperature[0] - other[0], MAX_ROOM_OFFSET_K)
        slab = _clamp(room.slab[0] - other[1], 1.0)
        rooms[room.key] = (round(temp, 4), round(slab, 4))
    tank = 0.0
    if optimised.hot_water is not None and reference.tank_c is not None:
        tank = _clamp(optimised.hot_water.temperature[0] - reference.tank_c, MAX_TANK_OFFSET_K)
    return TwinOffset(rooms=rooms, tank_c=round(tank, 3))


def cold_degree_hours(plan: Plan) -> float:
    """Degree-hours below the comfort floor in the plan's first step, all rooms."""
    dt = plan.step_minutes / 60.0
    total = 0.0
    for room in plan.rooms:
        if room.temperature:
            total += max(room.comfort_min - room.temperature[0], 0.0) * dt
    return total


@dataclass(frozen=True, slots=True)
class LedgerStep:
    """One planning step as booked in the ledger."""

    start: datetime
    spot: float
    spot_hour: float
    spot_day: float
    adder: float
    reference_kwh: float | None
    optimised_kwh: float | None
    house_kwh: float | None
    reference_cold_dh: float = 0.0
    optimised_cold_dh: float = 0.0
    control_enabled: bool = False


def settlement_spot(step: LedgerStep, contract: Contract, month_mean: float) -> float:
    """The spot price a step is billed at under `contract`, excluding VAT."""
    if contract == "quarterly":
        return step.spot
    if contract == "hourly":
        return step.spot_hour
    if contract == "daily":
        return step.spot_day
    return month_mean


def unit_price(
    step: LedgerStep, contract: Contract, pricing: EnergyPriceConfig, month_mean: float
) -> float:
    """What one kWh in this step costs under `contract`, in SEK incl. VAT."""
    vat = 1.0 if pricing.prices_include_vat else 1.0 + pricing.vat_rate
    if contract == "fixed":
        energy = pricing.fixed_price_ore / 100.0 * vat
    else:
        energy = settlement_spot(step, contract, month_mean) * vat
    return energy + step.adder


@dataclass(slots=True)
class DaySavings:
    day: date
    steps: int = 0
    house_kwh: float = 0.0
    reference_kwh: float = 0.0
    optimised_kwh: float = 0.0
    cost_now_sek: float = 0.0
    cost_quarter_sek: float = 0.0
    contract_effect_sek: float = 0.0
    control_effect_sek: float = 0.0
    reference_cold_dh: float = 0.0
    optimised_cold_dh: float = 0.0
    house_measured: bool = True

    @property
    def total_sek(self) -> float:
        return self.contract_effect_sek + self.control_effect_sek

    @property
    def coverage(self) -> float:
        return min(self.steps / 96.0, 1.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "coverage": round(self.coverage, 3),
            "house_kwh": round(self.house_kwh, 2),
            "reference_kwh": round(self.reference_kwh, 2),
            "optimised_kwh": round(self.optimised_kwh, 2),
            "cost_now_sek": round(self.cost_now_sek, 2),
            "cost_quarter_sek": round(self.cost_quarter_sek, 2),
            "cost_with_hemopt_sek": round(self.cost_now_sek - self.total_sek, 2),
            "contract_effect_sek": round(self.contract_effect_sek, 2),
            "control_effect_sek": round(self.control_effect_sek, 2),
            "total_sek": round(self.total_sek, 2),
            "reference_cold_dh": round(self.reference_cold_dh, 2),
            "optimised_cold_dh": round(self.optimised_cold_dh, 2),
            "house_measured": self.house_measured,
        }


def summarise(steps: list[LedgerStep], config: Config, tz) -> dict[str, Any]:
    """Per-day and total savings, from the current contract to quarterly + hemopt."""
    pricing = config.energy_price
    contract: Contract = pricing.contract

    # A monthly contract is billed at the month's mean spot. The ledger only
    # holds the steps it saw, which is the best available estimate of it.
    month_spots: dict[tuple[int, int], list[float]] = defaultdict(list)
    for step in steps:
        local = step.start.astimezone(tz)
        month_spots[(local.year, local.month)].append(step.spot)
    month_mean = {key: sum(values) / len(values) for key, values in month_spots.items()}

    # Without a whole-house meter the reference twin's heat pump energy has
    # to stand in for consumption, which understates the contract effect.
    # A day counts as measured when nearly all its steps were.
    measured_share: dict[date, float] = defaultdict(float)
    counts: dict[date, int] = defaultdict(int)
    for step in steps:
        day_key = step.start.astimezone(tz).date()
        counts[day_key] += 1
        measured_share[day_key] += step.house_kwh is not None
    use_meter = {day: measured_share[day] / counts[day] >= 0.8 for day in counts}

    days: dict[date, DaySavings] = {}
    for step in steps:
        local = step.start.astimezone(tz)
        day = days.setdefault(local.date(), DaySavings(day=local.date()))
        mean = month_mean[(local.year, local.month)]
        price_now = unit_price(step, contract, pricing, mean)
        price_quarter = unit_price(step, "quarterly", pricing, mean)

        day.steps += 1
        day.house_measured = use_meter[local.date()]
        consumption: float | None
        if day.house_measured:
            consumption = step.house_kwh
        else:
            consumption = step.reference_kwh
        if consumption is not None:
            day.house_kwh += consumption
            day.cost_now_sek += consumption * price_now
            day.cost_quarter_sek += consumption * price_quarter
            day.contract_effect_sek += consumption * (price_now - price_quarter)

        if step.reference_kwh is not None and step.optimised_kwh is not None:
            day.reference_kwh += step.reference_kwh
            day.optimised_kwh += step.optimised_kwh
            day.control_effect_sek += (step.reference_kwh - step.optimised_kwh) * price_quarter
            day.reference_cold_dh += step.reference_cold_dh
            day.optimised_cold_dh += step.optimised_cold_dh

    ordered = [days[key] for key in sorted(days)]
    totals = DaySavings(day=ordered[-1].day if ordered else date.today())
    for day in ordered:
        totals.steps += day.steps
        totals.house_kwh += day.house_kwh
        totals.reference_kwh += day.reference_kwh
        totals.optimised_kwh += day.optimised_kwh
        totals.cost_now_sek += day.cost_now_sek
        totals.cost_quarter_sek += day.cost_quarter_sek
        totals.contract_effect_sek += day.contract_effect_sek
        totals.control_effect_sek += day.control_effect_sek
        totals.reference_cold_dh += day.reference_cold_dh
        totals.optimised_cold_dh += day.optimised_cold_dh
        totals.house_measured = totals.house_measured and day.house_measured

    measured_days = totals.steps / 96.0
    total_payload = totals.as_dict()
    total_payload.pop("day")
    total_payload.pop("coverage")
    total_payload["measured_days"] = round(measured_days, 2)

    notes: list[str] = []
    if contract == "quarterly":
        notes.append("Du har redan kvartspris, så avtalseffekten är noll.")
    if ordered and not totals.house_measured:
        notes.append(
            "Ingen elmätare för hela huset är vald. Avtalseffekten räknas då bara "
            "på värmepumpen och blir för låg."
        )
    if 0 < measured_days < 7:
        notes.append("Mindre än en veckas data. Siffrorna stabiliseras efter några veckor.")

    per_day = totals.total_sek / measured_days if measured_days >= 1 else None
    return {
        "contract": contract,
        "contract_name": CONTRACT_NAMES.get(contract, contract),
        "target_name": CONTRACT_NAMES["quarterly"],
        "days": [day.as_dict() for day in ordered],
        "totals": total_payload,
        "saving_per_day_sek": round(per_day, 2) if per_day is not None else None,
        "notes": notes,
    }
