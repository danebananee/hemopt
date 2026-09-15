from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.config import HeatPumpConfig, HotWaterConfig, RoomConfig
from hemopt.optimizer import (
    HotWaterInput,
    OptimisationInput,
    PeakInput,
    RoomInput,
    solve,
    solve_baseline,
)
from hemopt.thermal import ThermalModel

TZ = ZoneInfo("Europe/Stockholm")


def build_problem(
    *,
    steps: int = 96,
    step_minutes: int = 15,
    cheap_window: tuple[int, int] = (0, 24),
    expensive_window: tuple[int, int] = (24, 40),
    peak_threshold: float = 4.0,
    peak_marginal: float = 13.5,
    priorities: dict[str, int] | None = None,
    with_hot_water: bool = True,
    base_load: float = 0.5,
    move_penalty: float = 0.0,
) -> OptimisationInput:
    start = datetime(2026, 1, 12, 0, 0, tzinfo=TZ)
    times = [start + timedelta(minutes=step_minutes * i) for i in range(steps)]

    price = []
    for index in range(steps):
        if cheap_window[0] <= index < cheap_window[1]:
            price.append(0.40)
        elif expensive_window[0] <= index < expensive_window[1]:
            price.append(4.50)
        else:
            price.append(1.20)

    priorities = priorities or {}
    rooms = []
    for key, name in [("vardagsrum", "Vardagsrum"), ("garage", "Garage")]:
        rooms.append(
            RoomInput(
                config=RoomConfig(
                    key=key,
                    name=name,
                    priority=priorities.get(key, 1),
                    temperature_entity=f"sensor.{key}_temperature",
                    comfort_min=20.0,
                    comfort_max=22.0,
                    max_preheat_offset=2.0,
                    max_setback_offset=2.0,
                ),
                model=ThermalModel(
                    tau_hours=80.0,
                    k_heat_per_hour=0.5,
                    k_gain_per_hour=0.0,
                    r_squared=0.9,
                    samples=1000,
                    fitted=True,
                ),
                initial_temperature=21.0,
                nominal_heat_kw=3.0,
            )
        )

    hot_water = None
    if with_hot_water:
        draws = [0.0] * steps
        for index in range(steps):
            if times[index].hour in (7, 19):
                draws[index] = 0.4
        hot_water = HotWaterInput(
            config=HotWaterConfig(
                tank_litres=180.0,
                min_temperature=42.0,
                target_temperature=52.0,
                max_temperature=58.0,
                reheat_power_kw=3.0,
            ),
            initial_temperature=52.0,
            draw_kwh=draws,
        )

    return OptimisationInput(
        start=start,
        step_minutes=step_minutes,
        times=times,
        price_sek_per_kwh=price,
        outdoor_c=[-5.0] * steps,
        base_load_kw=[base_load] * steps,
        rooms=rooms,
        heat_pump=HeatPumpConfig(max_thermal_kw=8.0, max_electrical_kw=3.0),
        peak=PeakInput(
            threshold_kw=peak_threshold,
            marginal_sek_per_kw=peak_marginal,
            in_window=[7 <= t.hour < 21 and t.weekday() < 5 for t in times],
        ),
        fuse_limit_kw=13.8,
        hot_water=hot_water,
        solver_time_limit_s=60.0,
        move_penalty_sek=move_penalty,
    )


def test_solver_returns_feasible_plan():
    plan = solve(build_problem())

    assert plan.status in {"kOptimal", "kTimeLimit"}
    assert len(plan.rooms) == 2
    assert len(plan.heat_pump_kw) == 96
    assert all(kw >= -1e-6 for kw in plan.heat_pump_kw)


def test_rooms_stay_inside_hard_bounds():
    plan = solve(build_problem())

    for room in plan.rooms:
        assert min(room.temperature) >= room.comfort_min - 2.0 - 1e-3
        assert max(room.temperature) <= room.comfort_max + 2.0 + 1e-3


def test_load_shifts_away_from_the_expensive_block():
    problem = build_problem()
    plan = solve(problem)

    cheap = sum(plan.heat_pump_kw[0:24])
    expensive = sum(plan.heat_pump_kw[24:40])

    assert cheap > expensive, "planner should buy heat before the price spike"


def test_preheating_happens_before_the_spike():
    problem = build_problem()
    plan = solve(problem)

    room = plan.rooms[0]
    before_spike = room.temperature[23]
    during_spike = room.temperature[38]

    assert before_spike > during_spike, "room should be charged up ahead of the spike"


def test_high_priority_room_is_protected():
    problem = build_problem(priorities={"vardagsrum": 3, "garage": 1})
    plan = solve(problem)

    protected = next(r for r in plan.rooms if r.key == "vardagsrum")
    sacrificial = next(r for r in plan.rooms if r.key == "garage")

    protected_deficit = sum(max(protected.comfort_min - t, 0.0) for t in protected.temperature)
    sacrificial_deficit = sum(
        max(sacrificial.comfort_min - t, 0.0) for t in sacrificial.temperature
    )

    assert protected_deficit <= sacrificial_deficit


def test_peak_tariff_caps_hourly_mean():
    cheap_but_peaky = build_problem(
        cheap_window=(0, 96),
        expensive_window=(0, 0),
        peak_threshold=1.5,
        peak_marginal=200.0,
    )
    plan = solve(cheap_but_peaky)

    billable = [hour for hour in plan.hour_peaks if hour.billable]
    assert billable, "January weekday horizon must contain billable hours"
    assert max(h.mean_kw for h in billable) <= 2.6, "peak fee should flatten billable hours"


def test_peak_tariff_is_ignored_outside_the_window():
    problem = build_problem(
        cheap_window=(0, 96),
        expensive_window=(0, 0),
        peak_threshold=1.0,
        peak_marginal=500.0,
    )
    plan = solve(problem)

    off_window = [h for h in plan.hour_peaks if not h.billable]
    assert any(h.mean_kw > 1.0 for h in off_window), "night hours are free and should be used"


def test_hot_water_is_charged_before_the_morning_draw():
    plan = solve(build_problem())
    assert plan.hot_water is not None

    charge_before_seven = sum(plan.hot_water.charge_fraction[0:28])
    assert charge_before_seven > 0.0
    assert min(plan.hot_water.temperature) >= 40.0


def test_fuse_limit_is_never_exceeded():
    plan = solve(build_problem(base_load=6.0))
    assert max(plan.total_power_kw) <= 13.8 + 1e-6


def test_hourly_hard_limit_is_respected():
    problem = build_problem()
    problem.peak.hard_limit_kw = 2.0
    plan = solve(problem)

    assert max(hour.mean_kw for hour in plan.hour_peaks) <= 2.0 + 1e-3


def test_solve_is_fast_enough_for_a_raspberry_pi():
    plan = solve(build_problem(steps=144))
    assert plan.solve_seconds < 25.0


@pytest.mark.parametrize("step_minutes", [15, 30, 60])
def test_supports_quarter_and_hourly_resolution(step_minutes: int):
    steps = int(24 * 60 / step_minutes)
    plan = solve(build_problem(steps=steps, step_minutes=step_minutes))
    assert len(plan.times) == steps


def _total_movement(plan) -> float:
    return sum(
        sum(abs(b - a) for a, b in zip(room.heat_fraction, room.heat_fraction[1:], strict=False))
        for room in plan.rooms
    )


def test_move_penalty_smooths_the_schedule():
    jittery = solve(build_problem(move_penalty=0.0))
    smooth = solve(build_problem(move_penalty=0.5))

    assert _total_movement(smooth) <= _total_movement(jittery) + 1e-9


def test_move_penalty_keeps_the_savings():
    """Smoothing must be close to free, otherwise it is not worth having."""
    problem = build_problem(move_penalty=0.0)
    unconstrained = solve(problem)
    smooth = solve(build_problem(move_penalty=0.08))

    dt = problem.step_hours
    cost = lambda plan: sum(  # noqa: E731
        p * dt * kw for p, kw in zip(problem.price_sek_per_kwh, plan.heat_pump_kw, strict=True)
    )
    assert cost(smooth) <= cost(unconstrained) * 1.05


@pytest.mark.parametrize("start_temperature", [12.0, 28.0])
def test_a_room_outside_its_band_still_gets_a_plan(start_temperature: float):
    """A cold snap or a changed comfort setting must not break planning."""
    problem = build_problem()
    problem.rooms[0].initial_temperature = start_temperature

    plan = solve(problem)

    assert plan.status == "kOptimal"
    room = plan.rooms[0]
    if start_temperature < room.comfort_min:
        assert room.temperature[-1] > start_temperature, "plan should recover the room"
    else:
        assert room.temperature[-1] < start_temperature, "plan should let the room cool"


def test_baseline_is_more_expensive_than_the_optimised_plan():
    problem = build_problem(move_penalty=0.08)
    plan = solve(problem)
    baseline = solve_baseline(problem)

    assert baseline.status in {"kOptimal", "kTimeLimit"}
    assert baseline.energy_cost_sek > plan.energy_cost_sek
