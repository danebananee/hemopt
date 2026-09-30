from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.thermal import (
    ThermalModel,
    ThermalSample,
    identify,
    nearest_value,
    previous_value,
    resample,
)

TZ = ZoneInfo("Europe/Stockholm")


def simulate(
    model: ThermalModel,
    hours: int = 24 * 14,
    step_minutes: int = 15,
    outdoor: float = -4.0,
    start_temperature: float = 21.0,
    setpoint: float = 21.0,
) -> list[ThermalSample]:
    """Run a thermostat against a known model to produce trainable history."""
    dt = step_minutes / 60.0
    moment = datetime(2026, 1, 1, tzinfo=TZ)
    indoor = start_temperature
    samples: list[ThermalSample] = []

    for index in range(int(hours / dt)):
        # A slow outdoor swing plus a hysteresis thermostat gives the fit both
        # the excitation and the variation it needs.
        outdoor_now = outdoor + 5.0 * math.sin(index / 96.0 * 2 * math.pi)
        demand = 1.0 if indoor < setpoint else 0.0
        samples.append(
            ThermalSample(moment=moment, indoor=indoor, outdoor=outdoor_now, heat_fraction=demand)
        )
        indoor = model.step(indoor, outdoor_now, demand, dt)
        moment += timedelta(minutes=step_minutes)

    return samples


def test_identify_recovers_known_parameters():
    truth = ThermalModel(
        tau_hours=70.0,
        k_heat_per_hour=0.5,
        k_gain_per_hour=0.02,
        r_squared=1.0,
        samples=0,
        fitted=True,
    )
    fitted = identify(simulate(truth))

    assert fitted.fitted
    assert fitted.tau_hours == pytest.approx(70.0, rel=0.1)
    assert fitted.k_heat_per_hour == pytest.approx(0.5, rel=0.1)
    assert fitted.r_squared > 0.95


def test_identify_separates_a_heavy_house_from_a_light_one():
    heavy = identify(
        simulate(
            ThermalModel(
                tau_hours=160.0,
                k_heat_per_hour=0.3,
                k_gain_per_hour=0.0,
                r_squared=1.0,
                samples=0,
                fitted=True,
            )
        )
    )
    light = identify(
        simulate(
            ThermalModel(
                tau_hours=35.0,
                k_heat_per_hour=0.8,
                k_gain_per_hour=0.0,
                r_squared=1.0,
                samples=0,
                fitted=True,
            )
        )
    )

    assert heavy.tau_hours > light.tau_hours * 2


def test_identify_falls_back_without_enough_data():
    prior = ThermalModel.default()
    fitted = identify([], prior=prior)

    assert not fitted.fitted
    assert fitted.tau_hours == prior.tau_hours


def test_identify_refuses_to_fit_a_room_that_never_heated():
    truth = ThermalModel(
        tau_hours=70.0,
        k_heat_per_hour=0.5,
        k_gain_per_hour=0.0,
        r_squared=1.0,
        samples=0,
        fitted=True,
    )
    samples = simulate(truth)
    for sample in samples:
        sample.heat_fraction = 0.0

    assert not identify(samples).fitted


def test_identified_coefficients_are_never_negative():
    """Noise must not produce a room that cools when heated."""
    truth = ThermalModel(
        tau_hours=90.0,
        k_heat_per_hour=0.4,
        k_gain_per_hour=0.0,
        r_squared=1.0,
        samples=0,
        fitted=True,
    )
    fitted = identify(simulate(truth))

    assert fitted.tau_hours > 0
    assert fitted.k_heat_per_hour >= 0
    assert fitted.k_gain_per_hour >= 0


def test_free_fall_time_grows_with_inertia():
    heavy = ThermalModel(160.0, 0.3, 0.0, 0.9, 100, True)
    light = ThermalModel(35.0, 0.8, 0.0, 0.9, 100, True)

    assert heavy.free_fall_hours(22.0, -5.0, 20.0) > light.free_fall_hours(22.0, -5.0, 20.0)


def test_free_fall_is_infinite_when_it_is_warm_outside():
    model = ThermalModel(80.0, 0.4, 0.0, 0.9, 100, True)
    assert model.free_fall_hours(22.0, 21.0, 20.0) == math.inf


def test_step_matches_the_discrete_coefficients_used_by_the_solver():
    model = ThermalModel(80.0, 0.4, 0.05, 0.9, 100, True)
    a, b, c = model.coefficients(0.25)

    direct = model.step(21.0, -5.0, 0.6, 0.25)
    linear = 21.0 + a * (-5.0 - 21.0) + b * 0.6 + c

    assert direct == pytest.approx(linear)


def test_resample_splits_on_logging_gaps():
    start = datetime(2026, 1, 1, tzinfo=TZ)
    samples = [ThermalSample(start + timedelta(minutes=15 * i), 21.0, -5.0, 0.0) for i in range(8)]
    samples += [
        ThermalSample(start + timedelta(hours=6) + timedelta(minutes=15 * i), 21.0, -5.0, 0.0)
        for i in range(8)
    ]

    segments = resample(samples, step_minutes=15, max_gap_minutes=60)
    assert len(segments) == 2


def _lagged_history(truth: ThermalModel, days: int = 21, seed: int = 7) -> list[ThermalSample]:
    """Two-node truth driven by a loop that changes every six hours."""
    import random

    rng = random.Random(seed)
    moment = datetime(2026, 1, 1, tzinfo=TZ)
    indoor, slab, loop = 21.0, 0.5, 0.5
    samples = []
    for index in range(96 * days):
        outdoor = -5.0 + 5.0 * math.sin(index / 96 * 2 * math.pi)
        if index % 24 == 0:
            loop = rng.choice([0.0, 0.3, 0.7, 1.0])
        samples.append(ThermalSample(moment, indoor + rng.gauss(0, 0.02), outdoor, loop))
        indoor, slab = truth.advance(indoor, slab, outdoor, loop, 0.25)
        moment += timedelta(minutes=15)
    return samples


@pytest.mark.parametrize("lag", [0.0, 0.75, 3.0])
def test_identification_recovers_the_floor_lag(lag):
    truth = ThermalModel(80.0, 0.6, 0.02, 1.0, 0, True, tau_slab_hours=lag)
    fitted = identify(_lagged_history(truth), floor_type="concrete")

    assert fitted.fitted
    assert fitted.tau_slab_hours == pytest.approx(lag, abs=0.3)
    assert fitted.tau_hours == pytest.approx(80.0, rel=0.1)
    assert fitted.k_heat_per_hour == pytest.approx(0.6, rel=0.1)
    # The four-hour prediction is what the planner relies on.
    assert fitted.rmse_4h is not None and fitted.rmse_4h < 0.15


def test_floor_type_is_only_a_starting_point():
    """A slab mislabelled as a light floor still gets its lag from the data."""
    truth = ThermalModel(80.0, 0.6, 0.02, 1.0, 0, True, tau_slab_hours=3.0)
    fitted = identify(_lagged_history(truth), floor_type="light")
    assert fitted.tau_slab_hours == pytest.approx(3.0, abs=0.5)


def test_floor_type_sets_the_starting_guess():
    concrete = ThermalModel.default("concrete")
    light = ThermalModel.default("light")
    assert concrete.tau_slab_hours > light.tau_slab_hours
    assert not concrete.fitted and not light.fitted


def test_advance_without_lag_matches_step():
    model = ThermalModel(80.0, 0.4, 0.05, 0.9, 100, True)
    indoor, slab = model.advance(21.0, 0.2, -5.0, 0.6, 0.25)
    assert indoor == pytest.approx(model.step(21.0, -5.0, 0.6, 0.25))
    assert slab == pytest.approx(0.6)


def test_steady_heat_fraction_holds_the_room():
    model = ThermalModel(80.0, 0.5, 0.01, 0.9, 100, True, tau_slab_hours=2.0)
    fraction = model.steady_heat_fraction(21.0, -5.0)
    indoor, _ = model.advance(21.0, fraction, -5.0, fraction, 0.25)
    assert indoor == pytest.approx(21.0, abs=1e-9)


def test_lookups_use_binary_search_semantics():
    base = datetime(2026, 1, 1, tzinfo=TZ)
    values = {base + timedelta(minutes=10 * i): float(i) for i in range(10)}
    ordered = sorted(values)
    assert nearest_value(values, ordered, base + timedelta(minutes=14)) == 1.0
    assert nearest_value(values, ordered, base + timedelta(minutes=16)) == 2.0
    assert nearest_value(values, ordered, base + timedelta(hours=5)) is None
    # A setpoint holds until changed: the last one before, not the nearest.
    assert previous_value(values, ordered, base + timedelta(minutes=19)) == 1.0
    assert previous_value(values, ordered, base - timedelta(minutes=1)) is None
