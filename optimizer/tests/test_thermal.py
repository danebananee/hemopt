from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.thermal import ThermalModel, ThermalSample, identify, resample

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
