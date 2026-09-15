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


@dataclass(slots=True)
class RoomInput:
    config: RoomConfig
    model: ThermalModel
    initial_temperature: float
    nominal_heat_kw: float


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
    for room in problem.rooms:
        # The band is widened to cover wherever the room actually is right
        # now. A room that has drifted outside its limits, or whose comfort
        # settings were just changed, must still produce a plan that walks it
        # back rather than reporting the whole house as infeasible.
        hard_floor = min(
            room.config.comfort_min - room.config.max_setback_offset,
            room.initial_temperature - 0.1,
        )
        hard_ceiling = max(
            room.config.comfort_max + room.config.max_preheat_offset,
            room.initial_temperature + 0.1,
        )

        temps = [solver.addVariable(lb=hard_floor, ub=hard_ceiling) for _ in range(steps + 1)]
        heats = [solver.addVariable(lb=0.0, ub=1.0) for _ in range(steps)]
        solver.addConstr(temps[0] == room.initial_temperature)

        a, b, c = room.model.coefficients(dt)
        for index in range(steps):
            solver.addConstr(
                temps[index + 1]
                == temps[index] * (1.0 - a) + a * problem.outdoor_c[index] + b * heats[index] + c
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

    # --- Grid limits and peak tariff -------------------------------------
    total_power = []
    for index in range(steps):
        total = solver.addVariable(lb=0.0, ub=problem.fuse_limit_kw)
        solver.addConstr(total == hp_power[index] + problem.base_load_kw[index])
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
        objective.append(problem.price_sek_per_kwh[index] * dt * hp_power[index])

    # --- Terminal value of stored heat -----------------------------------
    # Without this the plan empties every buffer at the horizon edge. Stored
    # energy is credited at the cheapest price in the horizon, so the credit
    # can never justify buying heat that is not worth it on its own.
    cheapest = min(problem.price_sek_per_kwh)
    for position, room in enumerate(problem.rooms):
        if room.model.k_heat_per_hour <= 0:
            continue
        capacity_kwh_per_k = room.nominal_heat_kw / room.model.k_heat_per_hour
        credit = cheapest / max(cop_heat[-1], 1e-6) * capacity_kwh_per_k
        objective.append(-credit * (room_temp[position][steps] - room.config.comfort_min))

    if tank_energy:
        objective.append(-(cheapest / max(cop_dhw[-1], 1e-6)) * tank_energy[steps])

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

    energy_cost = sum(problem.price_sek_per_kwh[i] * dt * hp_kw[i] for i in range(steps))
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
    )
    plan = solve(baseline)

    # Re-price the unoptimised schedule with the real tariff.
    dt = problem.step_hours
    energy = sum(
        price * dt * kw
        for price, kw in zip(problem.price_sek_per_kwh, plan.heat_pump_kw, strict=True)
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
