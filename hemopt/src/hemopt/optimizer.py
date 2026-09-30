"""Mixed-integer scheduler for heating, hot water and peak power.

The planner minimises, over a receding horizon:

    spot energy cost + peak-tariff cost + comfort penalty - value of stored heat

Room temperature, tank charge and the heat pump's electrical draw are all
linear in the decision variables, so the only integers are the heating versus
hot-water interlock. That keeps the model solvable in a second or two on a
Raspberry Pi.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import highspy

from .config import HeatPumpConfig, HotWaterConfig, RoomConfig
from .thermal import ThermalModel

_LOGGER = logging.getLogger(__name__)

# Penalty for pre-heating above the comfort band, as a fraction of the penalty
# for falling below it. Small but non-zero, so the solver only banks heat when
# the price spread actually pays for the mild overheating.
PREHEAT_DISCOMFORT_RATIO = 0.08

# Share of the cheapest heat in the horizon that stored heat is credited at
# when the horizon ends. Just under one, so storing is never free money.
TERMINAL_CREDIT_SHARE = 0.95

# How much worse a degree-hour outside the outer band (comfort plus allowed
# setback or pre-heat) is than one merely outside the comfort band.
OUTER_BAND_PENALTY = 10.0

# SEK per kWh the car is short of its target at departure.
EV_SHORTFALL_SEK_PER_KWH = 20.0


@dataclass(slots=True)
class RoomInput:
    config: RoomConfig
    model: ThermalModel
    initial_temperature: float
    nominal_heat_kw: float
    # How much the floor is currently giving off, as a fraction of full
    # output. None means unknown, in which case the steady-state value that
    # holds the room at its current temperature is assumed.
    initial_slab: float | None = None

    def slab_start(self, outdoor_c: float, sun: float = 0.0, wind_ms: float = 0.0) -> float:
        if self.initial_slab is not None:
            return max(0.0, min(1.0, self.initial_slab))
        return self.model.steady_heat_fraction(
            self.initial_temperature, outdoor_c, sun=sun, wind_ms=wind_ms
        )


@dataclass(slots=True)
class HotWaterInput:
    config: HotWaterConfig
    initial_temperature: float
    draw_kwh: list[float]
    # Step at which the tank must reach the legionella temperature. The caller
    # picks it, normally the cheapest quarter of the scheduled night, which
    # keeps the requirement a single linear constraint instead of an
    # "at least one step in this window" disjunction.
    legionella_step: int | None = None


@dataclass(slots=True)
class BatteryInput:
    """A home battery: energy in kWh, power in kW, efficiencies one-way."""

    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    efficiency_in: float
    efficiency_out: float
    min_kwh: float
    max_kwh: float
    initial_kwh: float
    wear_sek_per_kwh: float = 0.25


@dataclass(slots=True)
class EVInput:
    """An electric car while it is plugged in.

    `available` says per step whether the car is at the charger. By
    `deadline_step` (the step that ends at departure) it must hold
    `target_kwh`. With V2H it may also feed the house, never below
    `v2h_min_kwh`.
    """

    capacity_kwh: float
    max_charge_kw: float
    charge_efficiency: float
    initial_kwh: float
    available: list[bool]
    target_kwh: float
    deadline_step: int | None
    v2h_max_kw: float = 0.0
    v2h_min_kwh: float = 0.0
    discharge_efficiency: float = 0.9
    wear_sek_per_kwh: float = 0.25


@dataclass(slots=True)
class PeakInput:
    """Live state of the peak tariff for the current month."""

    threshold_kw: float
    marginal_sek_per_kw: float
    in_window: list[bool]
    elapsed_energy_kwh: float = 0.0
    elapsed_hours: float = 0.0
    hard_limit_kw: float | None = None


@dataclass(slots=True)
class OptimisationInput:
    start: datetime
    step_minutes: int
    times: list[datetime]
    price_sek_per_kwh: list[float]
    outdoor_c: list[float]
    base_load_kw: list[float]
    rooms: list[RoomInput]
    heat_pump: HeatPumpConfig
    peak: PeakInput
    fuse_limit_kw: float
    hot_water: HotWaterInput | None = None
    solver_time_limit_s: float = 30.0
    mip_gap: float = 0.01
    move_penalty_sek: float = 0.0
    # Sun reaching the house (0..1, see hemopt.weather) and wind in m/s per
    # step. Missing means the planner assumes no sun and no wind.
    sun: list[float] | None = None
    wind_ms: list[float] | None = None
    battery: BatteryInput | None = None
    ev: EVInput | None = None

    def sun_at(self, index: int) -> float:
        return self.sun[index] if self.sun else 0.0

    def wind_at(self, index: int) -> float:
        return self.wind_ms[index] if self.wind_ms else 0.0

    @property
    def steps(self) -> int:
        return len(self.times)

    @property
    def step_hours(self) -> float:
        return self.step_minutes / 60.0


@dataclass(slots=True)
class RoomPlan:
    key: str
    name: str
    priority: int
    temperature: list[float]
    setpoint: list[float]
    heat_fraction: list[float]
    comfort_min: float
    comfort_max: float
    # Floor output after each step, as a fraction of full output.
    slab: list[float] = field(default_factory=list)


@dataclass(slots=True)
class HotWaterPlan:
    temperature: list[float]
    charge_fraction: list[float]
    expected_draw_kwh: list[float]


@dataclass(slots=True)
class HourPeak:
    hour_start: datetime
    mean_kw: float
    billable: bool
    over_threshold_kw: float


@dataclass(slots=True)
class Plan:
    """The schedule plus the cost breakdown that justifies it."""

    start: datetime
    times: list[datetime]
    step_minutes: int
    rooms: list[RoomPlan]
    hot_water: HotWaterPlan | None
    heat_pump_kw: list[float]
    total_power_kw: list[float]
    price_sek_per_kwh: list[float]
    outdoor_c: list[float]
    hour_peaks: list[HourPeak]
    energy_cost_sek: float
    peak_cost_sek: float
    comfort_penalty_sek: float
    baseline_energy_cost_sek: float = 0.0
    baseline_peak_cost_sek: float = 0.0
    status: str = "optimal"
    solve_seconds: float = 0.0
    peak_threshold_kw: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Net power per step (positive charging) and state of charge after it.
    battery_kw: list[float] = field(default_factory=list)
    battery_soc_kwh: list[float] = field(default_factory=list)
    ev_kw: list[float] = field(default_factory=list)
    ev_soc_kwh: list[float] = field(default_factory=list)
    ev_shortfall_kwh: float = 0.0

    @property
    def total_cost_sek(self) -> float:
        return self.energy_cost_sek + self.peak_cost_sek

    @property
    def savings_sek(self) -> float:
        baseline = self.baseline_energy_cost_sek + self.baseline_peak_cost_sek
        return baseline - self.total_cost_sek

    def block_heating(self) -> list[bool]:
        """Steps where the plan wants no compressor heating at all.

        These are the steps an EXT input can enforce in hardware, which is far
        more reliable than hoping every thermostat honours its setpoint.
        """
        return [kw < 1e-3 for kw in self.heat_pump_kw]

    def step_at(self, moment: datetime) -> int:
        step = timedelta(minutes=self.step_minutes)
        for index, start in enumerate(self.times):
            if start <= moment < start + step:
                return index
        return 0 if moment < self.times[0] else len(self.times) - 1


class InfeasiblePlan(RuntimeError):
    pass


def _hour_groups(times: list[datetime]) -> dict[datetime, list[int]]:
    groups: dict[datetime, list[int]] = {}
    for index, moment in enumerate(times):
        hour = moment.replace(minute=0, second=0, microsecond=0)
        groups.setdefault(hour, []).append(index)
    return groups


def solve(problem: OptimisationInput) -> Plan:
    """Build and solve the schedule."""
    started = time.monotonic()
    steps = problem.steps
    dt = problem.step_hours
    heat_pump = problem.heat_pump

    if steps == 0:
        raise ValueError("optimisation horizon is empty")

    solver = highspy.Highs()
    solver.setOptionValue("output_flag", False)
    solver.setOptionValue("time_limit", problem.solver_time_limit_s)
    solver.setOptionValue("mip_rel_gap", problem.mip_gap)

    cop_heat = [heat_pump.cop(t, hot_water=False) for t in problem.outdoor_c]
    cop_dhw = [heat_pump.cop(t, hot_water=True) for t in problem.outdoor_c]

    objective = []

    # --- Room variables -------------------------------------------------
    room_temp: list[list] = []
    room_heat: list[list] = []
    room_slab: list[list] = []
    for room in problem.rooms:
        # The outer band (comfort plus the allowed setback and pre-heat) is
        # enforced by a steep penalty rather than as a hard limit. A room
        # warmed by a fire or the sun, or still being fed by a warm slab,
        # cannot always be kept inside it, and one such room must not make
        # the whole house's plan infeasible.
        hard_floor = min(
            room.config.comfort_min - room.config.max_setback_offset,
            room.initial_temperature - 0.1,
        )
        hard_ceiling = max(
            room.config.comfort_max + room.config.max_preheat_offset,
            room.initial_temperature + 0.1,
        )

        temps = [solver.addVariable(lb=-50.0, ub=60.0) for _ in range(steps + 1)]
        for index in range(1, steps + 1):
            under = solver.addVariable(lb=0.0)
            over = solver.addVariable(lb=0.0)
            solver.addConstr(temps[index] >= hard_floor - under)
            solver.addConstr(temps[index] <= hard_ceiling + over)
            objective.append(OUTER_BAND_PENALTY * room.config.comfort_weight * dt * (under + over))
        heats = [solver.addVariable(lb=0.0, ub=1.0) for _ in range(steps)]
        solver.addConstr(temps[0] == room.initial_temperature)

        # The loop charges the floor, the floor heats the room. Both are
        # linear, so the lag costs nothing in solve time.
        alpha = room.model.slab_alpha(dt)
        slabs = [solver.addVariable(lb=0.0, ub=1.0) for _ in range(steps + 1)]
        solver.addConstr(
            slabs[0] == room.slab_start(problem.outdoor_c[0], problem.sun_at(0), problem.wind_at(0))
        )

        _, b, c = room.model.coefficients(dt)
        for index in range(steps):
            # Wind raises the loss rate and sun adds heat; both are known
            # forecast numbers per step, so the constraint stays linear.
            a = min(dt * room.model.loss_rate(problem.wind_at(index)), 1.0)
            gain = c + dt * room.model.k_sun_per_hour * problem.sun_at(index)
            solver.addConstr(
                slabs[index + 1] == slabs[index] * (1.0 - alpha) + alpha * heats[index]
            )
            solver.addConstr(
                temps[index + 1]
                == temps[index] * (1.0 - a)
                + a * problem.outdoor_c[index]
                + b * slabs[index + 1]
                + gain
            )

        weight = room.config.comfort_weight
        for index in range(steps):
            below = solver.addVariable(lb=0.0)
            above = solver.addVariable(lb=0.0)
            solver.addConstr(temps[index + 1] >= room.config.comfort_min - below)
            solver.addConstr(temps[index + 1] <= room.config.comfort_max + above)
            objective.append(weight * dt * below)
            objective.append(weight * PREHEAT_DISCOMFORT_RATIO * dt * above)

        if problem.move_penalty_sek > 0:
            for index in range(1, steps):
                rise = solver.addVariable(lb=0.0)
                fall = solver.addVariable(lb=0.0)
                solver.addConstr(heats[index] - heats[index - 1] == rise - fall)
                objective.append(problem.move_penalty_sek * (rise + fall))

        room_temp.append(temps)
        room_heat.append(heats)
        room_slab.append(slabs)

    # --- Hot water ------------------------------------------------------
    dhw = problem.hot_water
    tank_energy: list = []
    dhw_charge: list = []
    if dhw is not None and dhw.config.enabled:
        kwh_per_degree = dhw.config.kwh_per_degree
        # The weekly legionella cycle deliberately runs hotter than the normal
        # ceiling, so the tank's modelled capacity has to reach that far.
        ceiling_c = dhw.config.max_temperature
        if dhw.legionella_step is not None:
            ceiling_c = max(ceiling_c, dhw.config.legionella_temperature)
        capacity = (ceiling_c - dhw.config.min_temperature) * kwh_per_degree
        target_energy = (
            dhw.config.target_temperature - dhw.config.min_temperature
        ) * kwh_per_degree
        initial_energy = max(
            (dhw.initial_temperature - dhw.config.min_temperature) * kwh_per_degree, 0.0
        )
        loss_per_step = dhw.config.standing_loss_kwh_per_day / 24.0 * dt

        # The floor is negative so the tank is allowed to dip below the minimum
        # temperature under protest rather than making the whole plan infeasible.
        tank_energy = [solver.addVariable(lb=-capacity, ub=capacity) for _ in range(steps + 1)]
        dhw_charge = [solver.addVariable(lb=0.0, ub=1.0) for _ in range(steps)]
        solver.addConstr(tank_energy[0] == min(initial_energy, capacity))

        for index in range(steps):
            solver.addConstr(
                tank_energy[index + 1]
                == tank_energy[index]
                + dt * dhw.config.reheat_power_kw * dhw_charge[index]
                - dhw.draw_kwh[index]
                - loss_per_step
            )

        for index in range(steps):
            shortfall = solver.addVariable(lb=0.0)
            below_target = solver.addVariable(lb=0.0)
            solver.addConstr(tank_energy[index + 1] >= -shortfall)
            solver.addConstr(tank_energy[index + 1] >= target_energy - below_target)
            objective.append(dhw.config.comfort_weight * shortfall)
            objective.append(0.05 * dhw.config.comfort_weight * below_target)

        if dhw.legionella_step is not None:
            index = min(max(dhw.legionella_step, 0), steps - 1)
            required = (
                dhw.config.legionella_temperature - dhw.config.min_temperature
            ) * kwh_per_degree
            miss = solver.addVariable(lb=0.0)
            solver.addConstr(tank_energy[index + 1] >= required - miss)
            objective.append(5.0 * dhw.config.comfort_weight * miss)

    # --- Heat pump power ------------------------------------------------
    heat_thermal = []
    hp_power = []
    for index in range(steps):
        thermal = sum(
            room_heat[r][index] * problem.rooms[r].nominal_heat_kw
            for r in range(len(problem.rooms))
        )
        heat_thermal.append(thermal)

        # Heating and the tank share one compressor, so they share its output.
        if dhw_charge:
            solver.addConstr(
                thermal + dhw_charge[index] * dhw.config.reheat_power_kw <= heat_pump.max_thermal_kw
            )
        else:
            solver.addConstr(thermal <= heat_pump.max_thermal_kw)

        electrical = thermal * (1.0 / cop_heat[index])
        if dhw_charge:
            electrical = electrical + dhw_charge[index] * (
                dhw.config.reheat_power_kw / cop_dhw[index]
            )
        power = solver.addVariable(lb=0.0, ub=heat_pump.max_electrical_kw)
        solver.addConstr(power == electrical)
        hp_power.append(power)

    if dhw_charge and heat_pump.strict_dhw_interlock:
        for index in range(steps):
            switch = solver.addBinary()
            solver.addConstr(heat_thermal[index] <= heat_pump.max_thermal_kw * (1 - switch))
            solver.addConstr(dhw_charge[index] <= switch)

    # --- Home battery -----------------------------------------------------
    battery = problem.battery
    bat_in: list = []
    bat_out: list = []
    bat_energy: list = []
    if battery is not None:
        bat_energy = [
            solver.addVariable(lb=battery.min_kwh, ub=battery.max_kwh) for _ in range(steps + 1)
        ]
        solver.addConstr(
            bat_energy[0] == min(max(battery.initial_kwh, battery.min_kwh), battery.max_kwh)
        )
        for index in range(steps):
            charge = solver.addVariable(lb=0.0, ub=battery.max_charge_kw)
            discharge = solver.addVariable(lb=0.0, ub=battery.max_discharge_kw)
            solver.addConstr(
                bat_energy[index + 1]
                == bat_energy[index]
                + dt * battery.efficiency_in * charge
                - dt * discharge * (1.0 / battery.efficiency_out)
            )
            # Wear is paid per kWh taken out, so small spreads are left alone.
            objective.append(battery.wear_sek_per_kwh * dt * discharge)
            bat_in.append(charge)
            bat_out.append(discharge)

    # --- Electric car -------------------------------------------------------
    ev = problem.ev
    ev_in: list = []
    ev_out: list = []
    ev_energy: list = []
    ev_short = None
    if ev is not None:
        ev_energy = [solver.addVariable(lb=0.0, ub=ev.capacity_kwh) for _ in range(steps + 1)]
        solver.addConstr(ev_energy[0] == min(max(ev.initial_kwh, 0.0), ev.capacity_kwh))
        floor = min(ev.v2h_min_kwh, ev.initial_kwh)
        for index in range(steps):
            here = ev.available[index] if index < len(ev.available) else False
            charge = solver.addVariable(lb=0.0, ub=ev.max_charge_kw if here else 0.0)
            discharge = solver.addVariable(lb=0.0, ub=ev.v2h_max_kw if here else 0.0)
            solver.addConstr(
                ev_energy[index + 1]
                == ev_energy[index]
                + dt * ev.charge_efficiency * charge
                - dt * discharge * (1.0 / ev.discharge_efficiency)
            )
            if ev.v2h_max_kw > 0:
                solver.addConstr(ev_energy[index + 1] >= floor)
                objective.append(ev.wear_sek_per_kwh * dt * discharge)
            ev_in.append(charge)
            ev_out.append(discharge)
        if ev.deadline_step is not None:
            deadline = min(max(ev.deadline_step, 0), steps - 1)
            ev_short = solver.addVariable(lb=0.0)
            solver.addConstr(ev_energy[deadline + 1] >= ev.target_kwh - ev_short)
            # Leaving with too little charge is far worse than any price.
            objective.append(EV_SHORTFALL_SEK_PER_KWH * ev_short)

    def storage_flow(index: int):
        flow = 0.0
        if bat_in:
            flow = flow + bat_in[index] - bat_out[index]
        if ev_in:
            flow = flow + ev_in[index] - ev_out[index]
        return flow

    # --- Grid limits and peak tariff -------------------------------------
    total_power = []
    for index in range(steps):
        # Batteries only serve the house: nothing is exported to the grid.
        total = solver.addVariable(lb=0.0, ub=problem.fuse_limit_kw)
        solver.addConstr(
            total == hp_power[index] + problem.base_load_kw[index] + storage_flow(index)
        )
        total_power.append(total)

    groups = _hour_groups(problem.times)
    peak = problem.peak
    daily_excess: dict[datetime, object] = {}
    hour_mean_expr: dict[datetime, object] = {}

    for position, (hour_start, indices) in enumerate(sorted(groups.items())):
        energy = sum(total_power[i] * dt for i in indices)
        elapsed = peak.elapsed_energy_kwh if position == 0 else 0.0
        mean_kw = energy + elapsed
        hour_mean_expr[hour_start] = mean_kw

        if peak.hard_limit_kw is not None:
            solver.addConstr(mean_kw <= peak.hard_limit_kw)

        if not peak.in_window[indices[0]]:
            continue

        day = hour_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if day not in daily_excess:
            daily_excess[day] = solver.addVariable(lb=0.0)
        solver.addConstr(mean_kw - peak.threshold_kw <= daily_excess[day])

    for excess in daily_excess.values():
        objective.append(peak.marginal_sek_per_kw * excess)

    # --- Energy cost ------------------------------------------------------
    for index in range(steps):
        objective.append(
            problem.price_sek_per_kwh[index] * dt * (hp_power[index] + storage_flow(index))
        )

    # --- Terminal value of stored heat -----------------------------------
    # Without this the plan empties every buffer at the horizon edge. Stored
    # heat is credited at slightly less than the cheapest heat anywhere in
    # the horizon, price divided by COP, so the credit can never by itself
    # justify buying heat. Crediting the cheapest *price* at the final step's
    # COP instead turned every step with a better COP than the last into a
    # reason to overheat, even with a flat price.
    heat_value = TERMINAL_CREDIT_SHARE * min(
        price / max(cop, 1e-6)
        for price, cop in zip(problem.price_sek_per_kwh, cop_heat, strict=True)
    )
    for position, room in enumerate(problem.rooms):
        if room.model.k_heat_per_hour <= 0:
            continue
        capacity_kwh_per_k = room.nominal_heat_kw / room.model.k_heat_per_hour
        objective.append(
            -heat_value
            * capacity_kwh_per_k
            * (room_temp[position][steps] - room.config.comfort_min)
        )
        # Heat already in the floor reaches the room after the horizon ends.
        # It was bought, so it is credited the same way as warmth in the air.
        stored = room.nominal_heat_kw * room.model.stored_heat_hours()
        if stored > 0:
            objective.append(-heat_value * stored * room_slab[position][steps])

    # Energy left in the batteries is worth what it would cost to put back
    # at the cheapest price, a little less, so it is never bought for itself.
    cheapest_price = min(problem.price_sek_per_kwh)
    if bat_energy:
        objective.append(
            -TERMINAL_CREDIT_SHARE * cheapest_price * battery.efficiency_out * bat_energy[steps]
        )
    if ev_energy:
        objective.append(-TERMINAL_CREDIT_SHARE * cheapest_price * ev_energy[steps])

    if tank_energy:
        tank_value = TERMINAL_CREDIT_SHARE * min(
            price / max(cop, 1e-6)
            for price, cop in zip(problem.price_sek_per_kwh, cop_dhw, strict=True)
        )
        objective.append(-tank_value * tank_energy[steps])

    solver.minimize(sum(objective))

    status = solver.getModelStatus()
    status_name = str(status).rsplit(".", 1)[-1]
    if status not in (
        highspy.HighsModelStatus.kOptimal,
        highspy.HighsModelStatus.kTimeLimit,
        highspy.HighsModelStatus.kSolutionLimit,
    ):
        raise InfeasiblePlan(f"solver returned {status_name}")

    values = solver.getSolution().col_value

    def value_of(variable) -> float:
        return float(values[variable.index])

    room_plans: list[RoomPlan] = []
    for position, room in enumerate(problem.rooms):
        temperatures = [value_of(v) for v in room_temp[position][1:]]
        fractions = [value_of(v) for v in room_heat[position]]
        setpoints = [
            min(max(t, room.config.setpoint_min), room.config.setpoint_max) for t in temperatures
        ]
        room_plans.append(
            RoomPlan(
                key=room.config.key,
                name=room.config.name,
                priority=room.config.priority,
                temperature=[round(t, 2) for t in temperatures],
                setpoint=[round(s, 1) for s in setpoints],
                heat_fraction=[round(f, 3) for f in fractions],
                comfort_min=room.config.comfort_min,
                comfort_max=room.config.comfort_max,
                slab=[round(value_of(v), 4) for v in room_slab[position][1:]],
            )
        )

    hot_water_plan = None
    if tank_energy:
        kwh_per_degree = dhw.config.kwh_per_degree
        hot_water_plan = HotWaterPlan(
            temperature=[
                round(dhw.config.min_temperature + value_of(v) / kwh_per_degree, 2)
                for v in tank_energy[1:]
            ],
            charge_fraction=[round(value_of(v), 3) for v in dhw_charge],
            expected_draw_kwh=[round(v, 3) for v in dhw.draw_kwh],
        )

    hp_kw = [round(value_of(v), 3) for v in hp_power]
    total_kw = [round(value_of(v), 3) for v in total_power]
    battery_kw = [round(value_of(a) - value_of(b), 3) for a, b in zip(bat_in, bat_out, strict=True)]
    battery_soc = [round(value_of(v), 3) for v in bat_energy[1:]]
    ev_kw = [round(value_of(a) - value_of(b), 3) for a, b in zip(ev_in, ev_out, strict=True)]
    ev_soc = [round(value_of(v), 3) for v in ev_energy[1:]]

    hour_peaks: list[HourPeak] = []
    for hour_start, indices in sorted(groups.items()):
        mean = sum(total_kw[i] * dt for i in indices)
        if hour_start == min(groups):
            mean += peak.elapsed_energy_kwh
        billable = peak.in_window[indices[0]]
        hour_peaks.append(
            HourPeak(
                hour_start=hour_start,
                mean_kw=round(mean, 3),
                billable=billable,
                over_threshold_kw=round(max(mean - peak.threshold_kw, 0.0), 3) if billable else 0.0,
            )
        )

    energy_cost = sum(
        problem.price_sek_per_kwh[i]
        * dt
        * (hp_kw[i] + (battery_kw[i] if battery_kw else 0.0) + (ev_kw[i] if ev_kw else 0.0))
        for i in range(steps)
    )
    peak_cost = sum(peak.marginal_sek_per_kw * value_of(excess) for excess in daily_excess.values())
    comfort_penalty = 0.0
    for position, room in enumerate(problem.rooms):
        weight = room.config.comfort_weight
        for temperature in room_plans[position].temperature:
            if temperature < room.config.comfort_min:
                comfort_penalty += weight * dt * (room.config.comfort_min - temperature)
            elif temperature > room.config.comfort_max:
                comfort_penalty += (
                    weight * PREHEAT_DISCOMFORT_RATIO * dt * (temperature - room.config.comfort_max)
                )

    return Plan(
        start=problem.start,
        times=list(problem.times),
        step_minutes=problem.step_minutes,
        rooms=room_plans,
        hot_water=hot_water_plan,
        heat_pump_kw=hp_kw,
        total_power_kw=total_kw,
        price_sek_per_kwh=list(problem.price_sek_per_kwh),
        outdoor_c=list(problem.outdoor_c),
        hour_peaks=hour_peaks,
        energy_cost_sek=round(energy_cost, 3),
        peak_cost_sek=round(peak_cost, 3),
        comfort_penalty_sek=round(comfort_penalty, 3),
        status=status_name,
        solve_seconds=round(time.monotonic() - started, 3),
        peak_threshold_kw=peak.threshold_kw,
        battery_kw=battery_kw,
        battery_soc_kwh=battery_soc,
        ev_kw=ev_kw,
        ev_soc_kwh=ev_soc,
        ev_shortfall_kwh=round(value_of(ev_short), 3) if ev_short is not None else 0.0,
    )


def solve_baseline(problem: OptimisationInput) -> Plan:
    """Solve the same horizon with price and peak signals removed.

    This is the thermostat-only reference the savings figure is measured
    against: same comfort bands, same physics, no reason to shift load. A flat
    price and a zero peak charge are enough to produce that behaviour, so the
    comfort limits stay untouched and the baseline can never be infeasible
    when the real plan was not.
    """
    flat_price = sum(problem.price_sek_per_kwh) / len(problem.price_sek_per_kwh)
    baseline = OptimisationInput(
        start=problem.start,
        step_minutes=problem.step_minutes,
        times=problem.times,
        price_sek_per_kwh=[flat_price] * problem.steps,
        outdoor_c=problem.outdoor_c,
        base_load_kw=problem.base_load_kw,
        rooms=problem.rooms,
        heat_pump=problem.heat_pump,
        peak=PeakInput(
            threshold_kw=problem.peak.threshold_kw,
            marginal_sek_per_kw=0.0,
            in_window=problem.peak.in_window,
            elapsed_energy_kwh=problem.peak.elapsed_energy_kwh,
            elapsed_hours=problem.peak.elapsed_hours,
            hard_limit_kw=None,
        ),
        fuse_limit_kw=problem.fuse_limit_kw,
        hot_water=problem.hot_water,
        solver_time_limit_s=problem.solver_time_limit_s,
        mip_gap=problem.mip_gap,
        move_penalty_sek=problem.move_penalty_sek,
        sun=problem.sun,
        wind_ms=problem.wind_ms,
        battery=problem.battery,
        ev=problem.ev,
    )
    plan = solve(baseline)

    # Re-price the unoptimised schedule with the real tariff.
    dt = problem.step_hours
    flows = [
        kw
        + (plan.battery_kw[i] if plan.battery_kw else 0.0)
        + (plan.ev_kw[i] if plan.ev_kw else 0.0)
        for i, kw in enumerate(plan.heat_pump_kw)
    ]
    energy = sum(
        price * dt * kw for price, kw in zip(problem.price_sek_per_kwh, flows, strict=True)
    )
    worst_by_day: dict[datetime, float] = {}
    for hour in plan.hour_peaks:
        if not hour.billable:
            continue
        day = hour.hour_start.replace(hour=0, minute=0, second=0, microsecond=0)
        worst_by_day[day] = max(worst_by_day.get(day, 0.0), hour.mean_kw)
    peak_cost = sum(
        problem.peak.marginal_sek_per_kw * max(value - problem.peak.threshold_kw, 0.0)
        for value in worst_by_day.values()
    )
    plan.energy_cost_sek = round(energy, 3)
    plan.peak_cost_sek = round(peak_cost, 3)
    return plan
