from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.config import WoodStoveConfig
from hemopt.thermal import ThermalModel, ThermalSample, identify
from hemopt.woodstove import build_report, detect_lit, recommend_windows

TZ = ZoneInfo("Europe/Stockholm")


def test_detect_lit_uses_hysteresis():
    cfg = WoodStoveConfig(
        enabled=True, temperature_entity="sensor.x", lit_above_c=40, lit_below_c=30
    )

    cold = detect_lit(cfg, binary_on=None, temperature_c=35, previously_lit=False)
    assert cold.lit is False

    lighting = detect_lit(cfg, binary_on=None, temperature_c=41, previously_lit=False)
    assert lighting.lit is True

    cooling = detect_lit(cfg, binary_on=None, temperature_c=35, previously_lit=True)
    assert cooling.lit is True

    out = detect_lit(cfg, binary_on=None, temperature_c=29, previously_lit=True)
    assert out.lit is False


def test_detect_lit_from_binary():
    cfg = WoodStoveConfig(enabled=True, binary_entity="binary_sensor.fire")
    assert detect_lit(cfg, binary_on=True, temperature_c=None, previously_lit=False).lit
    assert not detect_lit(cfg, binary_on=False, temperature_c=None, previously_lit=True).lit


def test_identify_recovers_stove_contribution():
    truth = ThermalModel(
        tau_hours=80.0,
        k_heat_per_hour=0.4,
        k_gain_per_hour=0.0,
        r_squared=1.0,
        samples=0,
        fitted=True,
        k_stove_per_hour=0.55,
    )
    dt = 0.25
    moment = datetime(2026, 1, 1, tzinfo=TZ)
    indoor = 20.0
    samples = []
    for index in range(14 * 24 * 4):
        outdoor = -5.0 + 4.0 * ((index % 96) / 96.0)
        heat = 1.0 if indoor < 21.0 else 0.0
        stove = 1.0 if 48 <= (index % 96) < 64 else 0.0  # evenings with fire
        samples.append(
            ThermalSample(
                moment=moment,
                indoor=indoor,
                outdoor=outdoor,
                heat_fraction=heat,
                stove_on=stove,
            )
        )
        indoor = truth.step(indoor, outdoor, heat, dt, stove_on=stove)
        moment += timedelta(minutes=15)

    fitted = identify(samples)
    assert fitted.fitted
    assert fitted.k_stove_per_hour == pytest.approx(0.55, rel=0.25)
    assert fitted.tau_hours == pytest.approx(80.0, rel=0.2)


def test_recommend_windows_picks_expensive_cold_stretch():
    start = datetime(2026, 1, 10, 0, tzinfo=TZ)
    times = [start + timedelta(minutes=15 * i) for i in range(96)]
    price = [0.4] * 96
    outdoor = [2.0] * 96
    pump = [1.0] * 96
    # Expensive cold evening with high pump demand.
    for i in range(68, 84):
        price[i] = 1.8
        outdoor[i] = -8.0
        pump[i] = 3.0

    windows = recommend_windows(
        times=times,
        price_sek=price,
        outdoor_c=outdoor,
        heat_pump_kw=pump,
        step_minutes=15,
    )
    assert windows
    assert windows[0].start.hour >= 16


def test_build_report_asks_for_the_first_marked_fire():
    cfg = WoodStoveConfig(enabled=True)
    report = build_report(
        cfg,
        detect_lit(cfg, binary_on=None, temperature_c=None, previously_lit=False),
        [],
        [],
    )
    assert report.status == "not_started"
    assert "Jag har tänt brasan" in report.summary


def test_a_learnt_stove_is_priced_in_kronor():
    start = datetime(2026, 1, 10, 0, tzinfo=TZ)
    times = [start + timedelta(minutes=15 * i) for i in range(96)]
    price = [0.5] * 96
    for i in range(68, 84):
        price[i] = 2.5
    displaceable = [2.0] * 96
    windows = recommend_windows(
        times=times,
        price_sek=price,
        outdoor_c=[-5.0] * 96,
        heat_pump_kw=[1.0] * 96,
        step_minutes=15,
        stove_kw=1.5,
        displaceable_kw=displaceable,
        cop=[3.0] * 96,
    )
    best = windows[0]
    assert best.start == times[68]
    assert best.end == times[83] + timedelta(minutes=15)
    # 4 h × 1.5 kW heat / COP 3 × 2.5 kr = 5 kr.
    assert best.saving_sek == pytest.approx(5.0, rel=0.01)


def test_a_fire_cannot_save_more_than_the_plan_would_heat():
    start = datetime(2026, 1, 10, 18, tzinfo=TZ)
    times = [start + timedelta(minutes=15 * i) for i in range(16)]
    windows = recommend_windows(
        times=times,
        price_sek=[2.0] * 16,
        outdoor_c=[-5.0] * 16,
        heat_pump_kw=[0.3] * 16,
        step_minutes=15,
        stove_kw=4.0,
        displaceable_kw=[0.3] * 16,
        cop=[3.0] * 16,
    )
    assert windows[0].saving_sek == pytest.approx(0.3 / 3.0 * 2.0 * 4, rel=0.01)


def test_nobody_is_told_to_light_a_fire_at_night():
    start = datetime(2026, 1, 10, 0, tzinfo=TZ)
    times = [start + timedelta(minutes=15 * i) for i in range(96)]
    price = [0.5] * 96
    for i in range(4, 20):  # expensive 01-05
        price[i] = 3.0
    windows = recommend_windows(
        times=times,
        price_sek=price,
        outdoor_c=[-10.0] * 96,
        heat_pump_kw=[2.0] * 96,
        step_minutes=15,
    )
    assert all(7 <= window.start.hour < 23 for window in windows)
